"""Assert-based self-check for evals/assertions.py (no server, no network).

Run: python evals/test_assertions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from assertions import evaluate_assertions  # noqa: E402
from models import EvalRun, Scenario, ToolCallRecord, TurnResult, UsageSummary  # noqa: E402


def _scenario(assertions: dict) -> Scenario:
    return Scenario(id="s", messages=["hi"], assertions=assertions)


def _run(tool_calls: list[ToolCallRecord] | None = None, duration_ms: int = 0, output: str = "ok") -> EvalRun:
    turn = TurnResult(
        input="hi",
        output=output,
        usage=UsageSummary(duration_ms=duration_ms, input_tokens=10, output_tokens=5),
        tool_calls=tool_calls or [],
    )
    return EvalRun(run_id="r", scenario_id="s", turns=[turn])


def test_required_tools_pass() -> None:
    run = _run([ToolCallRecord(tool="read_file")])
    failures = evaluate_assertions(_scenario({"required_tools": ["read_file"]}), run)
    assert failures == [], failures


def test_required_tools_missing() -> None:
    run = _run([ToolCallRecord(tool="write_file")])
    failures = evaluate_assertions(_scenario({"required_tools": ["read_file"]}), run)
    assert len(failures) == 1
    assert "required_tools" in failures[0]


def test_min_subagent_calls() -> None:
    run = _run([ToolCallRecord(tool="task"), ToolCallRecord(tool="task")])
    ok = evaluate_assertions(_scenario({"min_subagent_calls": 2}), run)
    assert ok == []
    bad = evaluate_assertions(_scenario({"min_subagent_calls": 3}), run)
    assert len(bad) == 1


def test_max_tool_errors() -> None:
    run = _run([ToolCallRecord(tool="read_file", error="boom")])
    ok = evaluate_assertions(_scenario({"max_tool_errors": 1}), run)
    assert ok == []
    bad = evaluate_assertions(_scenario({"max_tool_errors": 0}), run)
    assert len(bad) == 1


def test_max_latency_ms() -> None:
    run = _run(duration_ms=5000)
    ok = evaluate_assertions(_scenario({"max_latency_ms": 10000}), run)
    assert ok == []
    bad = evaluate_assertions(_scenario({"max_latency_ms": 1000}), run)
    assert len(bad) == 1


def test_no_assertions_means_no_failures() -> None:
    run = _run()
    assert evaluate_assertions(_scenario({}), run) == []


def test_response_contains_pass_case_insensitive() -> None:
    run = _run(output="The codename is Project-Marigold.")
    ok = evaluate_assertions(_scenario({"response_contains": ["project-marigold"]}), run)
    assert ok == []


def test_response_contains_fail() -> None:
    run = _run(output="I don't know.")
    bad = evaluate_assertions(_scenario({"response_contains": ["project-marigold"]}), run)
    assert len(bad) == 1
    assert "response_contains" in bad[0]


def test_sessions_message_groups() -> None:
    single = Scenario(id="a", messages=["x", "y"])
    assert single.message_groups() == [["x", "y"]]
    multi = Scenario(id="b", messages=[], sessions=[["x"], ["y", "z"]])
    assert multi.message_groups() == [["x"], ["y", "z"]]


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"\n{len(fns)} checks passed")
