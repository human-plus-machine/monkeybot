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


def test_tool_errors_count_self_corrected_same_tool_not_counted() -> None:
    run = _run([
        ToolCallRecord(tool="read_file", args_summary="bad/path.md", error="not found"),
        ToolCallRecord(tool="read_file", args_summary="good/path.md"),
    ])
    assert run.tool_errors_count() == 0
    ok = evaluate_assertions(_scenario({"max_tool_errors": 0}), run)
    assert ok == []


def test_tool_errors_count_unresolved_error_is_counted() -> None:
    run = _run([ToolCallRecord(tool="read_file", args_summary="bad/path.md", error="not found")])
    assert run.tool_errors_count() == 1
    bad = evaluate_assertions(_scenario({"max_tool_errors": 0}), run)
    assert len(bad) == 1


def test_tool_errors_count_ignores_success_on_a_different_tool() -> None:
    run = _run([
        ToolCallRecord(tool="read_file", args_summary="bad/path.md", error="not found"),
        ToolCallRecord(tool="glob", args_summary="**/*.md"),
    ])
    assert run.tool_errors_count() == 1


def test_tool_errors_count_multi_error_mixed_resolution() -> None:
    # First read_file error is resolved by the later successful read_file call; the
    # write_file error has no later successful write_file call, so it stays unresolved.
    run = _run([
        ToolCallRecord(tool="read_file", args_summary="bad/path.md", error="not found"),
        ToolCallRecord(tool="write_file", args_summary="out.txt", error="permission denied"),
        ToolCallRecord(tool="read_file", args_summary="good/path.md"),
    ])
    assert run.tool_errors_count() == 1


def test_tool_errors_count_forgives_by_tool_name_not_args() -> None:
    # Documents the coarse matching semantics: a later success on the same tool
    # forgives a prior error even against different args (different file paths).
    run = _run([
        ToolCallRecord(tool="write_file", args_summary="a.txt", error="disk full"),
        ToolCallRecord(tool="write_file", args_summary="b.txt"),
    ])
    assert run.tool_errors_count() == 0


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


def test_response_not_contains() -> None:
    run = _run(output="As an AI, I cannot do that.")
    bad = evaluate_assertions(_scenario({"response_not_contains": ["as an ai"]}), run)
    assert len(bad) == 1 and "response_not_contains" in bad[0]
    ok = evaluate_assertions(_scenario({"response_not_contains": ["refuse"]}), run)
    assert ok == []


def test_response_regex() -> None:
    run = _run(output="Total: $42.50 charged.")
    ok = evaluate_assertions(_scenario({"response_regex": r"\$\d+\.\d{2}"}), run)
    assert ok == []
    bad = evaluate_assertions(_scenario({"response_regex": [r"^\d+$"]}), run)
    assert len(bad) == 1 and "response_regex" in bad[0]


def test_response_regex_invalid_pattern_does_not_crash() -> None:
    run = _run(output="Total: $42.50 charged.")
    bad = evaluate_assertions(_scenario({"response_regex": [r"\$\d+\.\d{2}", r"(unclosed["]}), run)
    assert len(bad) == 1 and "invalid pattern" in bad[0]


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
