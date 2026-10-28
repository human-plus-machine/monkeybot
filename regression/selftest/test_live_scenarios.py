"""The T4 scenarios and suites load, and the live engine evaluates their keys."""

from __future__ import annotations

import pytest

from regression.t4.assertions import evaluate_assertions
from regression.t4.models import EvalRun, Scenario, ToolCallRecord, TurnResult, VerdictRecord
from regression.t4.scenario_loader import (
    EVAL_ROOT,
    SCENARIOS_ROOT,
    load_disk_scenarios,
    load_suite,
)

EXPECTED_CASES = {
    "file/core_read": "T4-FILE-01",
    "file/core_read_error": "T4-FILE-02",
    "file/core_write": "T4-FILE-03",
    "file/grep_absence_honesty": "T4-FILE-04",
    "shell/core_run_command": "T4-SHELL-01",
    "shell/core_run_command_sandbox": "T4-SHELL-02",
    "coding/fix_failing_test": "T4-CODE-01",
    "coding/add_feature_with_tests": "T4-CODE-02",
    "coding/navigate_repo": "T4-CODE-03",
    "coding_hard/fix_discount_rounding": "T4-CODE-04",
    "coding_hard/add_bulk_discount": "T4-CODE-05",
    "coding_hard/noisy_test_failure": "T4-CODE-06",
    "coding_hard/navigate_intl_rate": "T4-CODE-07",
    "coding_hard/rename_calc_tax": "T4-CODE-08",
    "coding_hard/honest_unfixable_failure": "T4-CODE-09",
    "coding_hard/multi_feature_long": "T4-CODE-10",
    "context/summarization_trigger": "T4-CTX-01",
    "context/post_compaction_continuity": "T4-CTX-02",
    "attachments/describe_image_live": "T4-ATT-01",
    "drift/path_constraint": "T4-VER-01",
    "drift/supersession": "T4-VER-02",
    "drift/preempt": "T4-VER-03",
    "drift/declares_done": "T4-VER-04",
    "drift/repeat_correction": "T4-VER-05",
    "subagents/dispatch_complete": "T4-SUB-01",
    "skills/skill_invocation": "T4-SKILL-01",
    "mcp/tool_invoke": "T4-MCP-01",
    "mcp/list_resources": "T4-MCP-02",
    "mcp/get_prompt": "T4-MCP-03",
    "mcp/enable_then_echo_live": "T4-MCP-04",
    "memory/recall_single_session": "T4-MEM-01",
    "memory/recall_cross_session": "T4-MEM-02",
    "memory/search_past_session": "T4-MEM-03",
    "multi_turn/task_tracking": "T4-TODO-01",
    "hooks/evidence_citation_live": "T4-HOOK-01",
    "subagents/persona_dispatch": "T4-SUB-02",
    "scheduler/http_one_tick_live": "T4-SCHED-01",
    "scheduler/model_starts_loop": "T4-SCHED-02",
    "config/reload_memory_off_live": "T4-CFG-01",
    "gateway/live_resume": "T4-GW-02",
}


@pytest.mark.parametrize("verifier_mode", ["on", "off"])
def test_every_scenario_loads_with_its_case_id(
    verifier_mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EVAL_VERIFIER_MODE", verifier_mode)
    loaded = {s.id: s.case for s in load_disk_scenarios()}
    assert loaded == EXPECTED_CASES


@pytest.mark.parametrize("verifier_mode", ["on", "off"])
def test_the_live_engine_evaluates_every_scenario_key(
    verifier_mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EVAL_VERIFIER_MODE", verifier_mode)
    empty = EvalRun(run_id="r", scenario_id="s", turns=[])
    for scenario in load_disk_scenarios():
        failures = evaluate_assertions(scenario, empty)
        assert not [f for f in failures if "not evaluated" in f], (scenario.id, failures)


def test_suites_name_existing_scenarios() -> None:
    for path in sorted((EVAL_ROOT / "suites").glob("*.yaml")):
        _, ids = load_suite(path)
        missing = [sid for sid in ids if not (SCENARIOS_ROOT / f"{sid}.yaml").is_file()]
        assert ids and not missing, (path.name, missing)
    _, smoke = load_suite(EVAL_ROOT / "suites" / "smoke.yaml")
    assert "file/core_read_error" in smoke
    assert not (SCENARIOS_ROOT / "tools").exists()


def _run(*tools: str, verdicts: list[VerdictRecord] | None = None) -> EvalRun:
    turn = TurnResult(
        input="hi",
        output="ok",
        tool_calls=[ToolCallRecord(tool=t) for t in tools],
        verdicts=verdicts or [],
    )
    return EvalRun(run_id="r", scenario_id="s", turns=[turn])


def _failures(assertions: dict[str, object], run: EvalRun) -> list[str]:
    return evaluate_assertions(Scenario(id="s", messages=["hi"], assertions=assertions), run)


def test_tool_calls_bounds_the_total_and_one_tool() -> None:
    run = _run("read_file", "task", "read_file")
    assert _failures({"tool_calls": {"min": 1, "max": 3, "task": {"min": 1}}}, run) == []
    assert _failures({"tool_calls": {"max": 2}}, run) == ["tool_calls: 3 > 2"]
    assert _failures({"tool_calls": {"min": 4}}, run) == ["tool_calls: 3 < 4"]
    assert _failures({"tool_calls": {"task": {"min": 2}}}, run) == ["tool_calls.task: 1 < 2"]
    assert _failures({"tool_calls": {"read_file": 1}}, run) == ["tool_calls.read_file: 2 > 1"]


def test_verdicts_bounds_and_status_in() -> None:
    stuck = VerdictRecord(status="stuck", severity="replan")
    assert _failures({"verdicts": {"max": 0}}, _run()) == []
    assert _failures({"verdicts": {"max": 0}}, _run(verdicts=[stuck])) == ["verdicts: 1 > 0"]
    assert _failures({"verdicts": {"min": 1}}, _run()) == ["verdicts: 0 < 1"]
    spec = {"verdicts": {"min": 1, "status_in": ["stuck", "off_track"]}}
    assert _failures(spec, _run(verdicts=[stuck])) == []
    drifting = VerdictRecord(status="drifting", severity="nudge")
    assert _failures(spec, _run(verdicts=[drifting])) == [
        "verdicts: unexpected statuses ['drifting']"
    ]


def test_unknown_keys_fail_instead_of_being_dropped() -> None:
    assert _failures({"no_errors": True}, _run()) == [
        "assertion keys not evaluated by the live runner: ['no_errors']"
    ]
