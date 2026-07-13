"""Assert-based self-check for evals/diff.py (no server, no network).

Run: python evals/test_diff.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from diff import diff_runs  # noqa: E402
from persistence import RunRecord, ScenarioAggregate, TurnLog, baseline_from_run  # noqa: E402


def _record(run_id: str, status: str, output: str, failures: list[str] | None = None) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        suite="smoke",
        scenarios=[
            ScenarioAggregate(
                scenario_id="tools/core_read",
                status=status,
                quality_scores={"turn_relevancy": 0.9 if status == "passed" else 0.5},
                requirement_failures=failures or [],
                turns=[TurnLog(input="read the file", output=output)],
            )
        ],
    )


def test_regression_pinpoints_turn() -> None:
    old = _record("a", "passed", "The heading is Browser.")
    new = _record("b", "failed", "I could not find that file.", ["required_tools: missing ['read_file']"])
    report, regression = diff_runs(old, new)
    assert regression
    assert "status passed -> failed" in report
    assert "required_tools" in report
    assert "read the file" in report  # the exact prompt behind the slip
    assert "-The heading is Browser." in report and "+I could not find that file." in report


def test_no_diff_is_quiet_and_passes() -> None:
    old = _record("a", "passed", "same")
    new = _record("b", "passed", "same")
    report, regression = diff_runs(old, new)
    assert not regression
    assert "No differences beyond noise." in report


def test_score_noise_ignored() -> None:
    old = _record("a", "passed", "same")
    new = _record("b", "passed", "same")
    new.scenarios[0].quality_scores["turn_relevancy"] = 0.87  # within 0.05 of 0.9
    _, regression = diff_runs(old, new)
    assert not regression


def test_baseline_strips_turns() -> None:
    baseline = baseline_from_run(_record("a", "passed", "secret turn text"))
    assert baseline.scenarios[0].turns == []


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"\n{len(fns)} checks passed")
