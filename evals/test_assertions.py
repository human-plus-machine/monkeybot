"""Assert-based self-check for evals/assertions.py (no server, no network).

Run: python evals/test_assertions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from assertions import evaluate_assertions  # noqa: E402
from models import EvalRun, Scenario, ToolCallRecord, TurnResult, UsageSummary, VerdictRecord  # noqa: E402


def _scenario(assertions: dict) -> Scenario:
    return Scenario(id="s", messages=["hi"], assertions=assertions)


def _run(
    tool_calls: list[ToolCallRecord] | None = None, duration_ms: int = 0, output: str = "ok"
) -> EvalRun:
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
    run = _run(
        [
            ToolCallRecord(tool="read_file", args_summary="bad/path.md", error="not found"),
            ToolCallRecord(tool="read_file", args_summary="good/path.md"),
        ]
    )
    assert run.tool_errors_count() == 0
    ok = evaluate_assertions(_scenario({"max_tool_errors": 0}), run)
    assert ok == []


def test_tool_errors_count_unresolved_error_is_counted() -> None:
    run = _run([ToolCallRecord(tool="read_file", args_summary="bad/path.md", error="not found")])
    assert run.tool_errors_count() == 1
    bad = evaluate_assertions(_scenario({"max_tool_errors": 0}), run)
    assert len(bad) == 1


def test_tool_errors_count_ignores_success_on_a_different_tool() -> None:
    run = _run(
        [
            ToolCallRecord(tool="read_file", args_summary="bad/path.md", error="not found"),
            ToolCallRecord(tool="glob", args_summary="**/*.md"),
        ]
    )
    assert run.tool_errors_count() == 1


def test_tool_errors_count_multi_error_mixed_resolution() -> None:
    # First read_file error is resolved by the later successful read_file call; the
    # write_file error has no later successful write_file call, so it stays unresolved.
    run = _run(
        [
            ToolCallRecord(tool="read_file", args_summary="bad/path.md", error="not found"),
            ToolCallRecord(tool="write_file", args_summary="out.txt", error="permission denied"),
            ToolCallRecord(tool="read_file", args_summary="good/path.md"),
        ]
    )
    assert run.tool_errors_count() == 1


def test_tool_errors_count_forgives_by_tool_name_not_args() -> None:
    # Documents the coarse matching semantics: a later success on the same tool
    # forgives a prior error even against different args (different file paths).
    run = _run(
        [
            ToolCallRecord(tool="write_file", args_summary="a.txt", error="disk full"),
            ToolCallRecord(tool="write_file", args_summary="b.txt"),
        ]
    )
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


def _verdict_run(*verdicts: VerdictRecord, path_args: list[str] | None = None) -> EvalRun:
    turn = TurnResult(
        input="hi",
        output="ok",
        tool_calls=[ToolCallRecord(tool="write_file", path_args=path_args or [])],
        verdicts=list(verdicts),
    )
    return EvalRun(run_id="r", scenario_id="s", turns=[turn])


def test_max_verdicts_zero_pass() -> None:
    run = _run()
    assert evaluate_assertions(_scenario({"max_verdicts": 0}), run) == []


def test_max_verdicts_fail() -> None:
    run = _verdict_run(VerdictRecord(status="drifting", severity="nudge"))
    bad = evaluate_assertions(_scenario({"max_verdicts": 0}), run)
    assert len(bad) == 1 and "max_verdicts" in bad[0]


def test_min_verdicts() -> None:
    run = _verdict_run(VerdictRecord(status="drifting", severity="none"))
    assert evaluate_assertions(_scenario({"min_verdicts": 1}), run) == []
    bad = evaluate_assertions(_scenario({"min_verdicts": 2}), run)
    assert len(bad) == 1 and "min_verdicts" in bad[0]


def test_verdict_status_in() -> None:
    run = _verdict_run(VerdictRecord(status="on_track", severity="none"))
    assert (
        evaluate_assertions(_scenario({"verdict_status_in": ["on_track", "drifting"]}), run) == []
    )
    bad = evaluate_assertions(
        _scenario({"verdict_status_in": ["on_track"]}),
        _verdict_run(VerdictRecord(status="stuck", severity="replan")),
    )
    assert len(bad) == 1 and "verdict_status_in" in bad[0]


def test_verdict_severity_max() -> None:
    run = _verdict_run(VerdictRecord(status="drifting", severity="nudge"))
    assert evaluate_assertions(_scenario({"verdict_severity_max": "nudge"}), run) == []
    bad = evaluate_assertions(_scenario({"verdict_severity_max": "none"}), run)
    assert len(bad) == 1 and "verdict_severity_max" in bad[0]


def test_files_not_touched() -> None:
    ok = _verdict_run(path_args=["src/app.py"])
    assert evaluate_assertions(_scenario({"files_not_touched": ["db/migrations/**"]}), ok) == []
    bad = _verdict_run(path_args=["db/migrations/001.sql"])
    failures = evaluate_assertions(_scenario({"files_not_touched": ["db/migrations/**"]}), bad)
    assert len(failures) == 1 and "files_not_touched" in failures[0]


def test_verifier_mode_nested_blocks() -> None:
    import os

    scenario = _scenario(
        {
            "metrics": ["response_relevancy"],
            "verifier_off": {"max_verdicts": 0},
            "verifier_on": {"min_verdicts": 1},
        }
    )
    with_verdict = _verdict_run(VerdictRecord(status="drifting", severity="none"))
    empty = _run()

    os.environ.pop("EVAL_VERIFIER_MODE", None)
    assert evaluate_assertions(scenario, with_verdict) == []

    os.environ["EVAL_VERIFIER_MODE"] = "off"
    try:
        bad = evaluate_assertions(scenario, with_verdict)
        assert any("max_verdicts" in f for f in bad)
        assert evaluate_assertions(scenario, empty) == []
    finally:
        os.environ.pop("EVAL_VERIFIER_MODE", None)

    os.environ["EVAL_VERIFIER_MODE"] = "on"
    try:
        assert evaluate_assertions(scenario, with_verdict) == []
        bad_on = evaluate_assertions(scenario, empty)
        assert any("min_verdicts" in f for f in bad_on)
    finally:
        os.environ.pop("EVAL_VERIFIER_MODE", None)


def _coding_run(
    calls: list[ToolCallRecord], workspace: Path | None = None, output: str = "done"
) -> EvalRun:
    run = _run(calls, output=output)
    run.workspace_dir = workspace
    return run


def _pytest(ok: bool | None) -> ToolCallRecord:
    return ToolCallRecord(
        tool="run_command",
        args_summary='{"argv": ["python", "-m", "pytest", "-q"]}',
        ok=ok,
    )


def test_verified_before_final_passes_when_last_verify_ok_and_no_later_write() -> None:
    calls = [
        ToolCallRecord(tool="replace_in_file", args_summary="{}", ok=True),
        _pytest(False),
        ToolCallRecord(tool="replace_in_file", args_summary="{}", ok=True),
        _pytest(True),
        ToolCallRecord(tool="read_file", args_summary="{}", ok=True),
    ]
    failures = evaluate_assertions(_scenario({"verified_before_final": True}), _run(calls))
    assert failures == [], failures


def test_verified_before_final_fails_without_any_verify() -> None:
    calls = [ToolCallRecord(tool="replace_in_file", args_summary="{}", ok=True)]
    bad = evaluate_assertions(_scenario({"verified_before_final": True}), _run(calls))
    assert len(bad) == 1 and "no verification command" in bad[0]


def test_verified_before_final_fails_when_last_verify_failed_or_unresolved() -> None:
    for ok in (False, None):
        bad = evaluate_assertions(
            _scenario({"verified_before_final": True}), _run([_pytest(True), _pytest(ok)])
        )
        assert len(bad) == 1 and "did not pass" in bad[0], ok


def test_verified_before_final_fails_when_write_follows_passing_verify() -> None:
    calls = [_pytest(True), ToolCallRecord(tool="write_file", args_summary="{}", ok=True)]
    bad = evaluate_assertions(_scenario({"verified_before_final": True}), _run(calls))
    assert len(bad) == 1 and "after the last passing verification" in bad[0]


def test_verified_before_final_custom_regex() -> None:
    cmd = ToolCallRecord(tool="run_command", args_summary='{"argv": ["./check.sh"]}', ok=True)
    scenario = _scenario({"verified_before_final": "check\\.sh"})
    assert evaluate_assertions(scenario, _run([cmd])) == []
    assert evaluate_assertions(_scenario({"verified_before_final": True}), _run([cmd]))


def test_verify_regex_matches_argv_list_and_command_forms() -> None:
    for summary in (
        '{"argv": ["npm", "test"]}',
        '{"command": "cargo test --all"}',
        '{"command": "make", "args": ["check"]}',
        '{"argv": ["uv", "run", "ruff", "check", "."]}',
    ):
        call = ToolCallRecord(tool="run_command", args_summary=summary, ok=True)
        scenario = _scenario({"verified_before_final": True})
        assert evaluate_assertions(scenario, _run([call])) == [], summary
    other = ToolCallRecord(tool="run_command", args_summary='{"argv": ["ls", "-la"]}', ok=True)
    assert evaluate_assertions(_scenario({"verified_before_final": True}), _run([other]))


def test_final_text_regex_checks_last_turn_only() -> None:
    first = TurnResult(input="a", output="calc/ops.py:12", usage=UsageSummary())
    last = TurnResult(input="b", output="nothing useful", usage=UsageSummary())
    run = EvalRun(run_id="r", scenario_id="s", turns=[first, last])
    bad = evaluate_assertions(_scenario({"final_text_regex": r"calc/ops\.py:\d+"}), run)
    assert len(bad) == 1 and "final_text_regex" in bad[0]
    run.turns[-1].output = "defined at calc/ops.py:17"
    assert evaluate_assertions(_scenario({"final_text_regex": r"calc/ops\.py:\d+"}), run) == []


def test_workspace_assertions_against_a_real_copy() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / "calc").mkdir()
        (ws / "calc" / "ops.py").write_text("def power(a, b):\n    return a**b\n")
        (ws / "tests").mkdir()
        (ws / "tests" / "test_ops.py").write_text("def test_ok():\n    assert True\n")
        scenario = Scenario(
            id="c",
            messages=["hi"],
            workspace_fixture="fixtures/coding/calc_repo",
            assertions={
                "workspace_file_contains": {"calc/ops.py": "def power("},
                "workspace_command_passes": "python -m pytest -q",
            },
        )
        assert evaluate_assertions(scenario, _coding_run([], ws)) == []

        scenario.assertions = {
            "workspace_file_contains": {"calc/ops.py": "def cube(", "missing.py": "x"},
            "workspace_command_passes": "python -c 'import sys; sys.exit(4)'",
        }
        bad = evaluate_assertions(scenario, _coding_run([], ws))
        assert len(bad) == 3, bad
        assert any("def cube(" in f for f in bad)
        assert any("missing.py" in f for f in bad)
        assert any("exit 4" in f for f in bad)


def test_workspace_unchanged_compares_with_fixture() -> None:
    import shutil
    import tempfile

    fixture = Path(__file__).resolve().parent / "fixtures" / "coding" / "calc_repo"
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "copy"
        shutil.copytree(fixture, ws)
        scenario = Scenario(
            id="c",
            messages=["hi"],
            workspace_fixture="fixtures/coding/calc_repo",
            assertions={"workspace_unchanged": ["tests/test_ops.py"]},
        )
        assert evaluate_assertions(scenario, _coding_run([], ws)) == []
        (ws / "tests" / "test_ops.py").write_text("# edited\n")
        bad = evaluate_assertions(scenario, _coding_run([], ws))
        assert len(bad) == 1 and "modified" in bad[0]
        (ws / "tests" / "test_ops.py").unlink()
        assert evaluate_assertions(scenario, _coding_run([], ws))


def test_fixture_calc_repo_green_passes_and_navigate_regex_accepts_workspace_prefix() -> None:
    import re
    import shutil
    import subprocess
    import tempfile

    fixture = Path(__file__).resolve().parent / "fixtures" / "coding" / "calc_repo_green"
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "copy"
        shutil.copytree(fixture, ws)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=ws,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout
    rx = re.compile(r"(?<![\w/])(?:eval-[\w-]+/)?calc/ops\.py:\d+")
    assert rx.search("`eval-coding-navigate_repo/calc/ops.py:16`")
    assert rx.search("calc/ops.py:16")
    assert not rx.search("build/calc/ops.py:12")
    assert not rx.search("eval-x/build/calc/ops.py:12")


def test_workspace_assertions_need_a_workspace() -> None:
    scenario = _scenario({"workspace_command_passes": "true"})
    bad = evaluate_assertions(scenario, _run())
    assert len(bad) == 1 and "no workspace directory" in bad[0]


def test_fixture_calc_repo_starts_with_exactly_one_failing_test() -> None:
    import shutil
    import subprocess
    import tempfile

    fixture = Path(__file__).resolve().parent / "fixtures" / "coding" / "calc_repo"
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "copy"
        shutil.copytree(fixture, ws)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=ws,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 1, proc.stdout
        assert "1 failed, 4 passed" in proc.stdout


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"\n{len(fns)} checks passed")
