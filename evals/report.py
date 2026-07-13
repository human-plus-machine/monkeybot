"""CLI: run (or load) a live eval suite, compare to a baseline, print a Markdown scorecard.

    uv run python -m evals.report --suite smoke --baseline evals/baselines/smoke.json

Needs a running agent (``AGENT_URL``, default ``http://127.0.0.1:8080``) unless ``--run``
points at an already-persisted run JSON. This is the same command CI runs on the two
documented trigger points (develop -> main PRs, lockfile-change PRs) with
``--fail-on-regression``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

# Sibling modules use bare imports (``from models import ...``); make that resolve whether this
# is invoked as ``python -m evals.report`` (repo root on sys.path) or ``python report.py``.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from assertions import evaluate_assertions  # noqa: E402
from models import EvalRun, Scenario  # noqa: E402
from persistence import (  # noqa: E402
    BaselineFile,
    RunRecord,
    ScenarioAggregate,
    TurnLog,
    baseline_from_run,
    build_suite_aggregate,
    git_metadata,
    load_baseline,
    load_run,
    save_baseline,
    save_run,
    utc_now_iso,
)
from runner import run_scenario_live  # noqa: E402
from scenario_loader import EVAL_ROOT, load_scenarios_by_id, load_suite  # noqa: E402
from scorer import (  # noqa: E402
    _read_agent_model_config,
    judge_expected,
    resolve_judge_provider_model,
    score_scenario,
)

SUITES_DIR = EVAL_ROOT / "suites"


def _model_metadata() -> tuple[str, str, str, str]:
    """(model_provider, model_name, judge_provider, judge_model)."""
    provider, name = _read_agent_model_config()
    judge_provider, judge_model = resolve_judge_provider_model()
    return provider, name, judge_provider, judge_model or name


async def _run_scenario(scenario: Scenario, agent_url: str) -> ScenarioAggregate:
    run_id = str(uuid.uuid4())
    try:
        turns = await run_scenario_live(scenario, agent_url)
    except Exception as exc:
        return ScenarioAggregate(
            scenario_id=scenario.id,
            description=scenario.description,
            tags=scenario.tags,
            status="errored",
            failure_reason=str(exc),
        )

    scores, details, pass_rate = await asyncio.to_thread(score_scenario, scenario, turns)
    reasons = {d["metric"]: d["reason"] for d in details if d.get("reason")}
    run = EvalRun(run_id=run_id, scenario_id=scenario.id, turns=turns, scores=scores)
    requirement_failures = evaluate_assertions(scenario, run)
    if judge_expected(scenario) and not scores:
        requirement_failures.append(
            "judge_scores_missing: metrics were requested but the judge returned no scores"
        )
    usage = run.usage_total()
    status = "failed" if requirement_failures else "passed"

    return ScenarioAggregate(
        scenario_id=scenario.id,
        description=scenario.description,
        tags=scenario.tags,
        status=status,
        messages_count=len(turns),
        quality_scores=scores,
        quality_reasons=reasons,
        pass_rate=pass_rate,
        latency_ms_total=usage.duration_ms,
        latency_ms_by_turn=[t.usage.duration_ms for t in turns],
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_tokens=usage.cached_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_creation_tokens,
        cost_usd=usage.cost_usd,
        tool_calls_count=run.tool_calls_count(),
        tool_calls_by_name=run.tool_calls_by_name(),
        tool_errors_count=run.tool_errors_count(),
        summarizations_count=run.summarizations_count(),
        subagent_calls_count=run.subagent_calls_count(),
        trace_ids=[t.trace_id for t in turns if t.trace_id],
        requirement_failures=requirement_failures,
        turns=[
            TurnLog(input=t.input, output=t.output, duration_ms=t.usage.duration_ms)
            for t in turns
        ],
    )


async def run_suite(suite_id: str, scenario_ids: list[str], agent_url: str) -> RunRecord:
    scenarios = load_scenarios_by_id(scenario_ids)
    started_at = utc_now_iso()
    aggregates = [await _run_scenario(s, agent_url) for s in scenarios]
    finished_at = utc_now_iso()

    model_provider, model_name, judge_provider, judge_model = _model_metadata()
    return RunRecord(
        run_id=str(uuid.uuid4()),
        suite=suite_id,
        started_at=started_at,
        finished_at=finished_at,
        agent_url=agent_url,
        model_provider=model_provider,
        model_name=model_name,
        judge_provider=judge_provider,
        judge_model=judge_model,
        scenarios=aggregates,
        suite_aggregate=build_suite_aggregate(aggregates),
        **git_metadata(),
    )


def _pct_increase(current: float, base: float) -> float:
    if base <= 0:
        return 0.0 if current <= 0 else float("inf")
    return (current - base) / base


def compare_to_baseline(
    baseline: BaselineFile | None, current: RunRecord, *, require_baseline: bool = False
) -> tuple[list[str], list[str]]:
    """Return ``(hard_failures, warnings)`` per the ticket's regression-gate table.

    Scenario errors/failures in ``current`` are always hard failures, baseline or not —
    that's this run's own result, not a comparison. ``require_baseline`` (a separate flag
    from ``--fail-on-regression``) controls only whether a *missing* baseline file is itself
    a hard failure: without a baseline there is nothing to diff token/cost/latency/p95
    against, so once a trusted baseline exists, pass ``--require-baseline`` too so a missing
    file doesn't silently skip that comparison. Until then, a missing baseline is a no-op
    warning-free pass here — the caller may still print a Warnings line for it.
    """
    hard: list[str] = []
    warn: list[str] = []

    for s in current.scenarios:
        if s.status == "errored":
            hard.append(f"{s.scenario_id}: scenario errored — {s.failure_reason or 'unknown error'}")
        elif s.status == "failed":
            hard.append(f"{s.scenario_id}: assertion failed — {'; '.join(s.requirement_failures)}")

    if baseline is None:
        if require_baseline:
            hard.append(
                "No baseline found — regression checks (tokens/cost/latency) cannot run. "
                "Run `--update-baseline` once this run is trustworthy, or pass --baseline."
            )
        return hard, warn

    base_by_id = {s.scenario_id: s for s in baseline.scenarios}
    for s in current.scenarios:
        b = base_by_id.get(s.scenario_id)
        if b is not None and b.status == "passed" and s.status != "passed":
            hard.append(f"{s.scenario_id}: passed on baseline, now {s.status}")

    cur, base = current.suite_aggregate, baseline.suite_aggregate
    cur_output = sum(s.output_tokens for s in current.scenarios)
    base_output = sum(s.output_tokens for s in baseline.scenarios)
    cur_tool_errors = sum(s.tool_errors_count for s in current.scenarios)
    base_tool_errors = sum(s.tool_errors_count for s in baseline.scenarios)

    if _pct_increase(cur.total_tokens, base.total_tokens) > 0.15:
        hard.append(f"total tokens: {base.total_tokens} -> {cur.total_tokens} (> 15% increase)")
    if _pct_increase(cur_output, base_output) > 0.20:
        hard.append(f"output tokens: {base_output} -> {cur_output} (> 20% increase)")
    if _pct_increase(cur.total_cost_usd, base.total_cost_usd) > 0.15:
        hard.append(f"cost: ${base.total_cost_usd:.4f} -> ${cur.total_cost_usd:.4f} (> 15% increase)")
    if _pct_increase(cur.p95_latency_ms, base.p95_latency_ms) > 0.20:
        hard.append(f"p95 latency: {base.p95_latency_ms:.0f}ms -> {cur.p95_latency_ms:.0f}ms (> 20% increase)")
    if cur_tool_errors > base_tool_errors:
        hard.append(f"tool errors: {base_tool_errors} -> {cur_tool_errors} (increase)")

    for metric, base_score in base.mean_quality_by_metric.items():
        cur_score = cur.mean_quality_by_metric.get(metric)
        if cur_score is not None and (base_score - cur_score) > 0.05:
            warn.append(f"{metric}: judge score dropped {base_score:.3f} -> {cur_score:.3f}")

    quality_improved = any(
        cur.mean_quality_by_metric.get(m, 0.0) > b for m, b in base.mean_quality_by_metric.items()
    )
    cost_or_latency_rose = (
        _pct_increase(cur.total_cost_usd, base.total_cost_usd) > 0
        or _pct_increase(cur.p95_latency_ms, base.p95_latency_ms) > 0
    )
    if quality_improved and cost_or_latency_rose:
        warn.append("Quality improved but cost/latency rose — explain the tradeoff in the PR")

    return hard, warn


def _fmt_delta(current: float, base: float | None, fmt: str = "{:.0f}") -> str:
    if base is None:
        return "—"
    return fmt.format(current - base)


def build_markdown_report(
    record: RunRecord, baseline: BaselineFile | None, hard: list[str], warn: list[str]
) -> str:
    cur = record.suite_aggregate
    base = baseline.suite_aggregate if baseline else None
    verdict = "FAIL" if hard else "PASS"
    mean_quality = (
        sum(cur.mean_quality_by_metric.values()) / len(cur.mean_quality_by_metric)
        if cur.mean_quality_by_metric
        else None
    )
    base_mean_quality = (
        sum(base.mean_quality_by_metric.values()) / len(base.mean_quality_by_metric)
        if base and base.mean_quality_by_metric
        else None
    )

    lines = [
        "## monkeybot Eval Scorecard",
        "",
        f"Run: `{record.run_id}`",
        f"Commit: `{record.git_sha or 'unknown'}`",
        f"Suite: `{record.suite}`",
        f"Model: `{record.model_provider}/{record.model_name}`",
        f"Judge: `{record.judge_provider}/{record.judge_model}`",
        "",
        "### Verdict",
        "",
        f"`{verdict}`",
        "",
        "### Summary",
        "",
        "| Metric | Baseline | Current | Delta |",
        "|---|---:|---:|---:|",
        (
            f"| Scenarios passed | "
            f"{base.passed_count if base else '—'}/{base.scenario_count if base else '—'} | "
            f"{cur.passed_count}/{cur.scenario_count} | "
            f"{_fmt_delta(cur.passed_count, base.passed_count if base else None)} |"
        ),
        (
            f"| Mean quality (informational) | "
            f"{f'{base_mean_quality:.3f}' if base_mean_quality is not None else '—'} | "
            f"{f'{mean_quality:.3f}' if mean_quality is not None else '—'} | "
            f"{_fmt_delta(mean_quality or 0.0, base_mean_quality, '{:+.3f}') if mean_quality is not None else '—'} |"
        ),
        (
            f"| Total tokens | {base.total_tokens if base else '—'} | {cur.total_tokens} | "
            f"{_fmt_delta(cur.total_tokens, base.total_tokens if base else None)} |"
        ),
        (
            f"| Cost | {f'${base.total_cost_usd:.4f}' if base else '—'} | ${cur.total_cost_usd:.4f} | "
            f"{_fmt_delta(cur.total_cost_usd, base.total_cost_usd if base else None, '{:+.4f}')} |"
        ),
        (
            f"| p95 latency | {f'{base.p95_latency_ms:.0f}ms' if base else '—'} | "
            f"{cur.p95_latency_ms:.0f}ms | "
            f"{_fmt_delta(cur.p95_latency_ms, base.p95_latency_ms if base else None)} |"
        ),
        (
            f"| Tool errors | "
            f"{sum(s.tool_errors_count for s in baseline.scenarios) if baseline else '—'} | "
            f"{sum(s.tool_errors_count for s in record.scenarios)} | — |"
        ),
        "",
        "### Hard failures",
        "",
    ]
    lines.extend(f"- {f}" for f in hard) if hard else lines.append("- None")
    lines += ["", "### Warnings (non-blocking)", ""]
    lines.extend(f"- {w}" for w in warn) if warn else lines.append("- None")
    lines += ["", "### Improvements", ""]
    improvements = [
        f"- {m}: {base.mean_quality_by_metric[m]:.3f} -> {score:.3f}"
        for m, score in cur.mean_quality_by_metric.items()
        if base and m in base.mean_quality_by_metric and score - base.mean_quality_by_metric[m] > 0.05
    ]
    lines.extend(improvements) if improvements else lines.append("- None")
    lines += ["", "### Trace Links", ""]
    trace_lines = [f"- `{s.scenario_id}`: `{tid}`" for s in record.scenarios for tid in s.trace_ids]
    lines.extend(trace_lines) if trace_lines else lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="smoke", help="Suite id under evals/suites/<id>.yaml")
    parser.add_argument("--baseline", type=Path, default=None, help="Baseline JSON path")
    parser.add_argument("--run", type=Path, default=None, help="Load an existing run JSON instead of executing")
    parser.add_argument("--agent-url", default=None, help="Override AGENT_URL env var")
    parser.add_argument("--fail-on-regression", action="store_true")
    parser.add_argument(
        "--require-baseline",
        action="store_true",
        help="Treat a missing baseline file as a hard failure (separate from --fail-on-regression, "
        "which always still blocks on this run's own scenario errors/failures regardless of "
        "baseline presence).",
    )
    parser.add_argument("--update-baseline", action="store_true", help="Write this run as the new baseline")
    args = parser.parse_args(argv)

    agent_url = args.agent_url or os.environ.get("AGENT_URL", "http://127.0.0.1:8080")
    baseline_path = args.baseline or (EVAL_ROOT / "baselines" / f"{args.suite}.json")

    if args.run:
        record = load_run(args.run)
    else:
        suite_path = SUITES_DIR / f"{args.suite}.yaml"
        suite_id, scenario_ids = load_suite(suite_path)
        record = asyncio.run(run_suite(suite_id, scenario_ids, agent_url))
        save_run(record)

    if args.update_baseline:
        save_baseline(baseline_from_run(record), baseline_path)
        print(f"Baseline written to {baseline_path}")
        return 0

    baseline = load_baseline(baseline_path)
    hard, warn = compare_to_baseline(baseline, record, require_baseline=args.require_baseline)
    print(build_markdown_report(record, baseline, hard, warn))

    if args.fail_on_regression and hard:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
