"""Run one T4 suite several times and compare each scenario to its keyed baseline.

    uv run python -m regression.t5 --suite coding_hard --trials 8 --agent-url http://127.0.0.1:8787

Reliability scenarios are pass^N. Capability scenarios get a Wilson 95% interval.
A missing baseline is reported as ``no baseline``, not as a pass. ``--update-baseline``
is the only flag that writes ``regression/t5/baselines/``.

Label tables of already-saved summaries still work:

    python -m regression.t5 --compare a.json b.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from regression.t4.preflight import AGENT_CONFIG, agent_model
from regression.t4.report import SUITES_DIR, run_suite
from regression.t4.scenario_loader import load_suite
from regression.t5.compare import (
    baseline_document,
    baseline_path,
    lookup,
    scenario_counts,
    write_baseline,
)
from regression.t5.stats import (
    ClassLists,
    ScenarioVerdict,
    default_trials,
    judge,
    load_classes,
)
from testkit.failure_ledger import LedgerWriter, format_banner

_VERDICTS = frozenset({"pass", "fail", "warn", "no baseline"})


def _summarize(label: str, suite: str, trials: list[list[dict[str, Any]]]) -> dict[str, Any]:
    by_scenario: dict[str, list[dict[str, Any]]] = {}
    for trial in trials:
        for row in trial:
            by_scenario.setdefault(row["scenario_id"], []).append(row)
    scenarios = {}
    for sid, rows in by_scenario.items():
        passed = sum(1 for row in rows if row["status"] == "passed")
        names: set[str] = set()
        for row in rows:
            names.update((row.get("tool_calls_by_name") or {}).keys())
        mean_by_name = {
            name: sum((row.get("tool_calls_by_name") or {}).get(name, 0) for row in rows)
            / len(rows)
            for name in sorted(names)
        }
        mempalace = [int(row.get("mempalace_calls") or 0) for row in rows]
        scenarios[sid] = {
            "runs": len(rows),
            "passed": passed,
            "pass_rate": passed / len(rows),
            "mean_tool_calls": sum(row["tool_calls_count"] for row in rows) / len(rows),
            "mean_tool_calls_by_name": mean_by_name,
            "mean_tool_errors": sum(row["tool_errors_count"] for row in rows) / len(rows),
            "mean_tokens": sum(row["input_tokens"] + row["output_tokens"] for row in rows)
            / len(rows),
            "mean_mempalace_calls": sum(mempalace) / len(rows),
            "mempalace_call_rate": sum(1 for count in mempalace if count > 0) / len(rows),
            "failures": [row["failure"] for row in rows if row["status"] != "passed"],
        }
    total = sum(item["runs"] for item in scenarios.values())
    passed = sum(item["passed"] for item in scenarios.values())
    return {
        "label": label,
        "suite": suite,
        "trials": len(trials),
        "overall_pass_rate": passed / total if total else 0.0,
        "scenarios": scenarios,
        "raw": trials,
    }


def judge_summary(
    summary: dict[str, Any],
    *,
    baseline: dict[str, Any] | None,
    classes: ClassLists,
) -> list[ScenarioVerdict]:
    """Score each summarized scenario. Does not read or write a baseline file."""
    verdicts: list[ScenarioVerdict] = []
    for scenario_id, row in summary["scenarios"].items():
        assignment = classes.assignment(scenario_id)
        counts = scenario_counts(baseline, scenario_id)
        baseline_passed, baseline_runs = counts if counts is not None else (None, None)
        verdicts.append(
            judge(
                scenario_id,
                passed=int(row["passed"]),
                runs=int(row["runs"]),
                klass=assignment.klass,
                case=assignment.case,
                baseline_passed=baseline_passed,
                baseline_runs=baseline_runs,
            )
        )
    return verdicts


def annotate(summary: dict[str, Any], verdicts: list[ScenarioVerdict]) -> None:
    """Add class, interval, baseline k/N, and verdict onto the summary in place."""
    for verdict in verdicts:
        row = summary["scenarios"][verdict.scenario_id]
        row["class"] = verdict.klass
        row["case"] = verdict.case
        row["baseline_passed"] = verdict.baseline_passed
        row["baseline_runs"] = verdict.baseline_runs
        row["verdict"] = verdict.verdict
        if verdict.interval is None:
            row["wilson95"] = None
        else:
            row["wilson95"] = [verdict.interval.low, verdict.interval.high]


def format_report(summary: dict[str, Any]) -> str:
    """Per-scenario table: k/N, Wilson 95% interval, baseline k/N, verdict."""
    lines = [
        "| scenario | class | k/N | Wilson 95% | baseline k/N | verdict |",
        "|---|---|---:|---|---:|---|",
    ]
    for scenario_id, row in summary["scenarios"].items():
        wilson = row.get("wilson95")
        interval = "—" if wilson is None else f"{wilson[0]:.3f}–{wilson[1]:.3f}"
        baseline_passed = row.get("baseline_passed")
        baseline_runs = row.get("baseline_runs")
        if baseline_passed is None or baseline_runs is None:
            baseline = "—"
        else:
            baseline = f"{baseline_passed}/{baseline_runs}"
        lines.append(
            f"| {scenario_id} | {row.get('class', 'capability')} | "
            f"{row['passed']}/{row['runs']} | {interval} | {baseline} | {row.get('verdict', '')} |"
        )
    return "\n".join(lines)


def record_verdicts(
    verdicts: list[ScenarioVerdict],
    ledger: LedgerWriter,
    *,
    suite: str,
    provider: str,
    model: str,
    config: str | None,
    agent_url: str,
) -> None:
    """A ``fail`` is a ledger failure with trial k/N and baseline k/N. A ``warn`` uses severity warn."""
    for verdict in verdicts:
        if verdict.verdict not in {"fail", "warn"}:
            continue
        if verdict.baseline_passed is None or verdict.baseline_runs is None:
            baseline_text = "none"
            baseline_pair = None
        else:
            baseline_text = f"{verdict.baseline_passed}/{verdict.baseline_runs}"
            baseline_pair = baseline_text
        path = f"regression/t4/scenarios/{verdict.scenario_id}.yaml"
        repro = f"uv run python -m regression.t5 --suite {suite} --trials {verdict.runs}"
        if config:
            repro = f"{repro} --config {config}"
        ledger.record_failure(
            severity="warn" if verdict.verdict == "warn" else "fail",
            tier="t5",
            case=verdict.case,
            nodeid=f"{path}::{verdict.scenario_id}",
            path=path,
            phase="call",
            outcome="failed",
            exc_type="TrialWarn" if verdict.verdict == "warn" else "TrialRegressed",
            message=(
                f"{verdict.scenario_id}: {verdict.verdict} "
                f"trial {verdict.passed}/{verdict.runs} baseline {baseline_text}"
            ),
            scenario={"mode": "live", "driver": "trials", "class": verdict.klass},
            trials={
                "k": verdict.passed,
                "n": verdict.runs,
                "baseline_k": verdict.baseline_passed,
                "baseline_n": verdict.baseline_runs,
                "trial": f"{verdict.passed}/{verdict.runs}",
                "baseline": baseline_pair,
                "verdict": verdict.verdict,
            },
            live={
                "provider": provider,
                "model": model,
                "agent_url": agent_url,
                "suite": suite,
                "config": config,
            },
            repro=repro,
        )


def exit_status(verdicts: list[ScenarioVerdict]) -> int:
    """1 when any scenario fails. 2 when none fail but one has no baseline. 0 for pass and warn."""
    if any(verdict.verdict not in _VERDICTS for verdict in verdicts):
        raise ValueError("unknown verdict")
    if any(verdict.verdict == "fail" for verdict in verdicts):
        return 1
    if any(verdict.verdict == "no baseline" for verdict in verdicts):
        return 2
    return 0


def publish(
    summary: dict[str, Any],
    verdicts: list[ScenarioVerdict],
    *,
    suite: str,
    provider: str,
    model: str,
    config: str | None,
    trials: int,
    update_baseline: bool,
    directory: Path | None = None,
    ledger: LedgerWriter | None = None,
    agent_url: str = "",
) -> Path | None:
    """Write the baseline only when ``update_baseline`` is set. Otherwise record fail and warn."""
    if update_baseline:
        path = baseline_path(suite, provider, model, config, directory=directory)
        document = baseline_document(
            suite=suite,
            provider=provider,
            model=model,
            config=config,
            trials=trials,
            scenarios={
                scenario_id: (int(row["passed"]), int(row["runs"]))
                for scenario_id, row in summary["scenarios"].items()
            },
        )
        write_baseline(path, document)
        return path
    if ledger is not None:
        record_verdicts(
            verdicts,
            ledger,
            suite=suite,
            provider=provider,
            model=model,
            config=config,
            agent_url=agent_url,
        )
    return None


def _mempalace_calls(by_name: dict[str, int]) -> int:
    return sum(count for name, count in by_name.items() if "mempalace" in name)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    suite_id, scenario_ids = load_suite(SUITES_DIR / f"{args.suite}.yaml")
    trials: list[list[dict[str, Any]]] = []
    for n in range(args.trials):
        record = await run_suite(suite_id, scenario_ids, args.agent_url, args.scenario_delay_sec)
        rows = []
        for scenario in record.scenarios:
            by_name = dict(scenario.tool_calls_by_name)
            rows.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "status": scenario.status,
                    "failure": "; ".join(scenario.requirement_failures)
                    or scenario.failure_reason
                    or "",
                    "tool_calls_count": scenario.tool_calls_count,
                    "tool_calls_by_name": by_name,
                    "mempalace_calls": _mempalace_calls(by_name),
                    "tool_errors_count": scenario.tool_errors_count,
                    "input_tokens": scenario.input_tokens,
                    "output_tokens": scenario.output_tokens,
                    "latency_ms": scenario.latency_ms_total,
                    "final_text": scenario.turns[-1].output[-600:] if scenario.turns else "",
                }
            )
        trials.append(rows)
        done = sum(1 for row in rows if row["status"] == "passed")
        print(f"[{args.label}] trial {n + 1}/{args.trials}: {done}/{len(rows)} passed", flush=True)
        _write_summary(args.out, _summarize(args.label, suite_id, trials))
    return _summarize(args.label, suite_id, trials)


def _write_summary(path: str, summary: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def compare(paths: list[str]) -> str:
    docs = [json.loads(Path(path).read_text(encoding="utf-8")) for path in paths]
    scenario_ids = sorted({sid for doc in docs for sid in doc["scenarios"]})
    header = "| scenario | " + " | ".join(doc["label"] for doc in docs) + " |"
    lines = [header, "|---|" + "---:|" * len(docs)]
    for sid in scenario_ids:
        cells = []
        for doc in docs:
            row = doc["scenarios"].get(sid)
            cells.append("—" if row is None else f"{row['passed']}/{row['runs']}")
        lines.append(f"| {sid} | " + " | ".join(cells) + " |")
    total = []
    for doc in docs:
        runs = sum(row["runs"] for row in doc["scenarios"].values())
        passed = sum(row["passed"] for row in doc["scenarios"].values())
        total.append(f"**{passed}/{runs}** ({doc['overall_pass_rate']:.0%})")
    lines.append("| **overall** | " + " | ".join(total) + " |")
    tokens = []
    for doc in docs:
        runs = sum(row["runs"] for row in doc["scenarios"].values())
        tokens.append(
            f"{sum(row['mean_tokens'] * row['runs'] for row in doc['scenarios'].values()) / max(runs, 1):,.0f}"
        )
    lines.append("| mean tokens / run | " + " | ".join(tokens) + " |")
    return "\n".join(lines)


def _config_slug(raw: str | None) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    name = Path(str(raw).strip()).name
    for suffix in (".yaml", ".yml"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    prefix = "monkeybot."
    if name.startswith(prefix):
        name = name[len(prefix) :]
    return name or None


def _finish_ledger(
    ledger: LedgerWriter | None,
    summary: dict[str, Any] | None,
    exit_code: int,
) -> None:
    if ledger is None:
        return
    scenarios = summary["scenarios"] if summary is not None else {}
    failed = sum(1 for row in scenarios.values() if row.get("verdict") == "fail")
    passed = sum(1 for row in scenarios.values() if row.get("verdict") == "pass")
    ledger.finish(
        exit_status=exit_code,
        counts={
            "passed": passed,
            "failed": failed,
            "errors": 1 if summary is None and ledger.failures else 0,
            "skipped": 0,
            "xfailed": 0,
            "xpassed_strict": 0,
        },
        outcomes=[
            (
                f"regression/t4/scenarios/{scenario_id}.yaml::{scenario_id}",
                "failed" if row.get("verdict") in {"fail", "no baseline"} else "passed",
            )
            for scenario_id, row in scenarios.items()
        ],
    )
    if ledger.failures:
        print(format_banner(ledger.failures, ledger.shown_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="coding_hard")
    parser.add_argument("--trials", type=int, default=None)
    parser.add_argument("--agent-url", default="http://127.0.0.1:8787")
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", default="trials.json")
    parser.add_argument(
        "--scenario-delay-sec",
        type=float,
        default=float(os.environ.get("EVAL_SCENARIO_DELAY_SEC", "0")),
    )
    parser.add_argument("--compare", nargs="+", metavar="JSON")
    parser.add_argument(
        "--config",
        default=None,
        help="Verifier config slug for a drift baseline (verifier-tracker or verifier-judge).",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Write this run as the keyed baseline. Nothing else writes that file.",
    )
    parser.add_argument("--ledger-dir", type=Path, default=None)
    parser.add_argument("--no-ledger", action="store_true")
    args = parser.parse_args(argv)
    if args.compare:
        print(compare(args.compare))
        return 0

    classes = load_classes()
    suite_path = SUITES_DIR / f"{args.suite}.yaml"
    suite_id, scenario_ids = load_suite(suite_path)
    if args.trials is None:
        args.trials = default_trials(scenario_ids, classes)
    if args.trials < 1:
        print("--trials must be positive", file=sys.stderr)
        return 2

    config = _config_slug(args.config)
    if config is None and suite_id == "drift":
        config = _config_slug(os.environ.get("EVAL_VERIFIER_CONFIG"))
    if suite_id != "drift":
        config = None

    os.environ.setdefault("MONKEYBOT_AGENT_CONFIG", str(AGENT_CONFIG))
    provider, model = agent_model()
    use_ledger = not args.no_ledger and os.environ.get("MONKEYBOT_TEST_LEDGER") != "0"
    command = ["python", "-m", "regression.t5", *(sys.argv[1:] if argv is None else argv)]
    ledger: LedgerWriter | None = None
    if use_ledger and not args.update_baseline:
        ledger = LedgerWriter(args.ledger_dir)
        ledger.start(
            argv=command,
            live={
                "provider": provider,
                "model": model,
                "agent_url": args.agent_url,
                "suite": suite_id,
                "config": config,
            },
        )
    try:
        summary = asyncio.run(_run(args))
    except BaseException as exc:
        if ledger is not None:
            ledger.record_failure(
                severity="fail",
                tier="t5",
                nodeid=f"regression.t5::{suite_id}",
                phase="call",
                outcome="error",
                exc_type=type(exc).__name__,
                message=f"runner crashed: {exc}",
                live={
                    "provider": provider,
                    "model": model,
                    "agent_url": args.agent_url,
                    "suite": suite_id,
                },
            )
            _finish_ledger(ledger, None, 130 if isinstance(exc, KeyboardInterrupt) else 1)
        raise

    document = lookup(suite_id, provider, model, config)
    verdicts = judge_summary(
        summary,
        baseline=None if args.update_baseline else document,
        classes=classes,
    )
    annotate(summary, verdicts)
    if args.update_baseline:
        for row in summary["scenarios"].values():
            row["verdict"] = "updated"
            row["baseline_passed"] = None
            row["baseline_runs"] = None
    _write_summary(args.out, summary)
    written = publish(
        summary,
        verdicts,
        suite=suite_id,
        provider=provider,
        model=model,
        config=config,
        trials=args.trials,
        update_baseline=args.update_baseline,
        ledger=ledger,
        agent_url=args.agent_url,
    )
    print(format_report(summary))
    if written is not None:
        print(f"Baseline written to {written}")
    code = 0 if args.update_baseline else exit_status(verdicts)
    _finish_ledger(ledger, summary, code)
    printable = {key: value for key, value in summary.items() if key != "raw"}
    print(json.dumps(printable, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
