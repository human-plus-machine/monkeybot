"""Run one eval suite several times against one gateway and aggregate per-scenario results.

    uv run python -m evals.trials --suite coding_hard --trials 3 --agent-url http://127.0.0.1:8787 \\
        --label branch-coding --out evals/runs/branch-coding.json

A single LLM run says little; pass rates over repeated trials are what separate "the harness
helps" from noise. Compare labels with ``python -m evals.trials --compare a.json b.json ...``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from report import SUITES_DIR, run_suite  # noqa: E402
from scenario_loader import load_suite  # noqa: E402


def _summarize(label: str, suite: str, trials: list[list[dict]]) -> dict:
    by_scenario: dict[str, list[dict]] = {}
    for trial in trials:
        for row in trial:
            by_scenario.setdefault(row["scenario_id"], []).append(row)
    scenarios = {}
    for sid, rows in by_scenario.items():
        passed = sum(1 for r in rows if r["status"] == "passed")
        scenarios[sid] = {
            "runs": len(rows),
            "passed": passed,
            "pass_rate": passed / len(rows),
            "mean_tool_calls": sum(r["tool_calls_count"] for r in rows) / len(rows),
            "mean_tool_errors": sum(r["tool_errors_count"] for r in rows) / len(rows),
            "mean_tokens": sum(r["input_tokens"] + r["output_tokens"] for r in rows) / len(rows),
            "failures": [r["failure"] for r in rows if r["status"] != "passed"],
        }
    total = sum(s["runs"] for s in scenarios.values())
    passed = sum(s["passed"] for s in scenarios.values())
    return {
        "label": label,
        "suite": suite,
        "trials": len(trials),
        "overall_pass_rate": passed / total if total else 0.0,
        "scenarios": scenarios,
        "raw": trials,
    }


async def _run(args: argparse.Namespace) -> dict:
    suite_id, scenario_ids = load_suite(SUITES_DIR / f"{args.suite}.yaml")
    trials: list[list[dict]] = []
    for n in range(args.trials):
        record = await run_suite(suite_id, scenario_ids, args.agent_url, args.scenario_delay_sec)
        rows = [
            {
                "scenario_id": s.scenario_id,
                "status": s.status,
                "failure": "; ".join(s.requirement_failures) or s.failure_reason or "",
                "tool_calls_count": s.tool_calls_count,
                "tool_errors_count": s.tool_errors_count,
                "input_tokens": s.input_tokens,
                "output_tokens": s.output_tokens,
                "latency_ms": s.latency_ms_total,
                "final_text": s.turns[-1].output[-600:] if s.turns else "",
            }
            for s in record.scenarios
        ]
        trials.append(rows)
        done = sum(1 for r in rows if r["status"] == "passed")
        print(f"[{args.label}] trial {n + 1}/{args.trials}: {done}/{len(rows)} passed", flush=True)
        Path(args.out).write_text(
            json.dumps(_summarize(args.label, suite_id, trials), indent=2), encoding="utf-8"
        )
    return _summarize(args.label, suite_id, trials)


def compare(paths: list[str]) -> str:
    docs = [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]
    scenario_ids = sorted({sid for d in docs for sid in d["scenarios"]})
    header = "| scenario | " + " | ".join(d["label"] for d in docs) + " |"
    lines = [header, "|---|" + "---:|" * len(docs)]
    for sid in scenario_ids:
        cells = []
        for d in docs:
            s = d["scenarios"].get(sid)
            cells.append("—" if s is None else f"{s['passed']}/{s['runs']}")
        lines.append(f"| {sid} | " + " | ".join(cells) + " |")
    total = []
    for d in docs:
        runs = sum(s["runs"] for s in d["scenarios"].values())
        passed = sum(s["passed"] for s in d["scenarios"].values())
        total.append(f"**{passed}/{runs}** ({d['overall_pass_rate']:.0%})")
    lines.append("| **overall** | " + " | ".join(total) + " |")
    tokens = []
    for d in docs:
        runs = sum(s["runs"] for s in d["scenarios"].values())
        tokens.append(
            f"{sum(s['mean_tokens'] * s['runs'] for s in d['scenarios'].values()) / max(runs, 1):,.0f}"
        )
    lines.append("| mean tokens / run | " + " | ".join(tokens) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="coding_hard")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--agent-url", default="http://127.0.0.1:8787")
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", default="trials.json")
    parser.add_argument("--scenario-delay-sec", type=float, default=0.0)
    parser.add_argument("--compare", nargs="+", metavar="JSON")
    args = parser.parse_args(argv)
    if args.compare:
        print(compare(args.compare))
        return 0
    result = asyncio.run(_run(args))
    print(json.dumps({k: v for k, v in result.items() if k != "raw"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
