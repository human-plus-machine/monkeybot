"""§5.3.1 assertion keys: one positive and one negative each, plus load-time rules."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from textwrap import dedent

import pytest
from pydantic import ValidationError

from monkeybot.core.llm.provider import Message
from monkeybot.core.messages.tool_integrity import interrupted_tool_result_text
from monkeybot.core.runtime.events import TurnComplete, UsageTotals
from monkeybot.core.types.content_blocks import (
    SystemNotification,
    Text,
    Thinking,
    ToolRequest,
    ToolResponse,
)
from regression.schema.assertions import (
    WORKSPACE_COMMAND_TIMEOUT_SEC,
    ProviderRequest,
    RunView,
    StoreView,
    evaluate,
)
from regression.schema.scenario import load_scenario
from regression.t4.assertions import evaluate_assertions
from regression.t4.models import (
    EvalRun,
    Scenario,
    ToolCallRecord,
    TurnResult,
    UsageSummary,
    VerdictRecord,
)
from regression.t4.scenario_loader import FIXTURES_ROOT


def _ok(assertions: dict[str, object], run: RunView) -> None:
    failures = evaluate(assertions, run)
    assert failures == [], failures


def _bad(assertions: dict[str, object], run: RunView, key: str) -> None:
    failures = evaluate(assertions, run)
    assert any(key in item for item in failures), failures


def _run(*events: object, **kwargs: object) -> RunView:
    return RunView(events=list(events), **kwargs)  # type: ignore[arg-type]


def _call(
    call_id: str,
    tool: str,
    args: dict[str, object] | None = None,
    *,
    error: str | None = None,
    result: str = "",
    resolved_path: str | None = None,
) -> list[dict[str, object]]:
    started: dict[str, object] = {
        "type": "ToolCallStarted",
        "call_id": call_id,
        "tool": tool,
        "args": args or {},
    }
    if resolved_path is not None:
        started["resolved_path"] = resolved_path
    finished: dict[str, object] = {
        "type": "ToolCallResult",
        "call_id": call_id,
        "tool": tool,
        "error": error,
        "result": result,
    }
    return [started, finished]


def _envelope(code: str) -> str:
    return json.dumps({"ok": False, "details": {"code": code}})


def _load(tmp_path: Path, mode: str, assertions: str, extra: str = "") -> None:
    body = f"case: T2-LOOP-07\nmode: {mode}\n{extra}assertions:\n{assertions}"
    path = tmp_path / "case.yaml"
    path.write_text(dedent(body).strip() + "\n", encoding="utf-8")
    load_scenario(path, root=tmp_path)


def _reject(tmp_path: Path, mode: str, assertions: str, match: str, extra: str = "") -> None:
    with pytest.raises(ValidationError, match=match):
        _load(tmp_path, mode, assertions, extra)


def test_turn_completed_pass_and_fail() -> None:
    _ok({"turn_completed": True}, _run(TurnComplete()))
    _bad(
        {"turn_completed": True}, _run({"type": "AssistantDelta", "delta": "hi"}), "turn_completed"
    )


def test_no_errors_pass_and_fail() -> None:
    _ok({"no_errors": True}, _run({"type": "TurnComplete"}))
    _bad({"no_errors": True}, _run({"type": "Error", "error": "boom"}), "no_errors")


def test_error_contains_pass_and_fail() -> None:
    run = _run({"type": "Error", "error": "disk full"})
    _ok({"error_contains": "disk"}, run)
    _bad({"error_contains": "permission"}, run, "error_contains")


def test_events_sequence_is_an_ordered_subsequence() -> None:
    run = _run(
        {"type": "ToolCallStarted", "tool": "read_file"},
        {"type": "AssistantDelta", "delta": "x"},
        {"type": "ToolCallResult", "tool": "read_file"},
        {"type": "TurnComplete"},
    )
    _ok(
        {"events_sequence": ["ToolCallStarted", {"type": "TurnComplete"}]},
        run,
    )
    _bad({"events_sequence": ["TurnComplete", "ToolCallStarted"]}, run, "events_sequence")


def test_events_absent_pass_and_fail() -> None:
    run = _run({"type": "TurnComplete"})
    _ok({"events_absent": ["Error"]}, run)
    _bad({"events_absent": ["TurnComplete"]}, run, "events_absent")


def test_event_fields_value_or_regex() -> None:
    run = _run({"type": "ToolCallStarted", "tool": "read_file", "call_id": "c1"})
    _ok({"event_fields": {"ToolCallStarted": {"tool": {"regex": "read_.*"}}}}, run)
    _bad({"event_fields": {"ToolCallStarted": {"tool": "write_file"}}}, run, "event_fields")


def test_event_fields_descends_into_usage_totals() -> None:
    usage = UsageTotals(input_tokens=4, output_tokens=6)
    run = _run({"type": "TurnComplete", "usage": usage})
    _ok(
        {
            "event_fields": {
                "TurnComplete": {
                    "usage": {
                        "input_tokens": 4,
                        "output_tokens": 6,
                        "cached_tokens": 0,
                    }
                }
            }
        },
        run,
    )
    _bad(
        {"event_fields": {"TurnComplete": {"usage": {"input_tokens": 9}}}},
        run,
        "event_fields",
    )


def test_provider_request_thinking_budget() -> None:
    run = RunView(
        provider_requests=[
            ProviderRequest(thinking_budget=None),
            ProviderRequest(thinking_budget=64),
        ]
    )
    _ok(
        {
            "provider_request": [
                {"n": 1, "thinking_budget": None},
                {"n": 2, "thinking_budget": 64},
            ]
        },
        run,
    )
    _bad(
        {"provider_request": [{"n": 2, "thinking_budget": 0}]},
        run,
        "thinking_budget",
    )


def test_interventions_are_exact_and_ordered() -> None:
    run = _run(
        {"type": "HarnessIntervention", "intervention": "final_answer_gate", "detail": "once"},
        {"type": "HarnessIntervention", "intervention": "doom_loop", "detail": "retry"},
    )
    _ok(
        {
            "interventions": [
                "final_answer_gate",
                {"intervention": "doom_loop", "detail": "retry"},
            ]
        },
        run,
    )
    _bad(
        {"interventions": ["doom_loop", "final_answer_gate"]},
        run,
        "interventions",
    )
    _bad(
        {"interventions": ["final_answer_gate"]},
        run,
        "interventions",
    )


def test_interventions_include_is_the_loose_form() -> None:
    run = _run(
        {"type": "HarnessIntervention", "intervention": "doom_loop", "detail": "retry"},
        {"type": "HarnessIntervention", "intervention": "final_answer_gate", "detail": "once"},
    )
    _ok({"interventions_include": ["final_answer_gate"]}, run)
    _bad({"interventions_include": ["max_turns"]}, run, "interventions_include")


def test_provider_calls_is_an_exact_count() -> None:
    run = RunView(provider_calls=2)
    _ok({"provider_calls": 2}, run)
    _bad({"provider_calls": 1}, run, "provider_calls")


def test_provider_request_matchers() -> None:
    request = ProviderRequest(
        tools=["read_file"],
        system="You are the agent.",
        messages=["hello there"],
        vertex_google_search=False,
    )
    run = RunView(provider_requests=[request])
    _ok(
        {
            "provider_request": [
                {
                    "n": 1,
                    "tools_include": ["read_file"],
                    "tools_exclude": ["web_search"],
                    "tools_empty": False,
                    "system_contains": "agent",
                    "system_not_contains": "secret",
                    "messages_contain": "hello",
                    "messages_not_contain": "secret-body",
                    "no_vertex_google_search": True,
                    "model": "",
                }
            ]
        },
        run,
    )
    _bad(
        {"provider_request": [{"tools_include": ["write_file"]}]},
        run,
        "provider_request",
    )


def test_required_and_forbidden_tools() -> None:
    run = _run(*_call("c1", "read_file", {"path": "README.md"}))
    _ok({"required_tools": ["read_file"]}, run)
    _bad({"required_tools": ["write_file"]}, run, "required_tools")
    _ok({"forbidden_tools": ["web_search"]}, run)
    _bad({"forbidden_tools": ["read_file"]}, run, "forbidden_tools")


def test_tool_categories_used_is_a_subset() -> None:
    run = _run(
        *_call("c1", "read_file", {"path": "a.txt"}),
        *_call("c2", "write_file", {"path": "a.txt"}),
    )
    _ok({"tool_categories_used": ["read"]}, run)
    only_read = _run(*_call("c1", "read_file", {"path": "a.txt"}))
    _bad({"tool_categories_used": ["read", "write"]}, only_read, "tool_categories_used")


def test_tool_calls_min_max_and_per_name() -> None:
    run = _run(*_call("c1", "read_file"), *_call("c2", "read_file"))
    _ok({"tool_calls": {"min": 1, "max": 3, "read_file": 2}}, run)
    _bad({"tool_calls": {"read_file": 1}}, run, "tool_calls")


def test_tool_error_codes_present_and_absent() -> None:
    run = _run(*_call("c1", "read_file", error="missing", result=_envelope("not_found")))
    _ok({"tool_error_codes": ["not_found"]}, run)
    _bad({"tool_error_codes": ["denied"]}, run, "tool_error_codes")
    _ok({"tool_error_codes_absent": ["denied"]}, run)
    _bad({"tool_error_codes_absent": ["not_found"]}, run, "tool_error_codes_absent")


def test_tool_error_code_on_error_field_only() -> None:
    run = _run(*_call("c1", "replace_in_file", error=_envelope("read_before_write")))
    _ok({"tool_error_codes": ["read_before_write"]}, run)


def test_non_json_error_string_adds_no_code() -> None:
    run = _run(*_call("c1", "read_file", error="missing"))
    _bad({"tool_error_codes": ["missing"]}, run, "tool_error_codes")
    _ok({"tool_error_codes_absent": ["missing"]}, run)
    _bad({"max_tool_errors": 0}, run, "max_tool_errors")


def test_max_tool_errors_forgives_only_the_same_canonical_target() -> None:
    same_path = _run(
        *_call("c1", "read_file", {"path": "./a.txt"}, error="missing"),
        *_call("c2", "read_file", {"path": "a.txt"}),
    )
    _ok({"max_tool_errors": 0}, same_path)
    same_command = _run(
        *_call("c1", "run_command", {"argv": ["pytest", "-q"]}, error="fail"),
        *_call("c2", "run_command", {"command": "pytest", "args": ["-q"]}),
    )
    _ok({"max_tool_errors": 0}, same_command)
    other_path = _run(
        *_call("c1", "read_file", {"path": "a.txt"}, error="missing"),
        *_call("c2", "read_file", {"path": "b.txt"}),
    )
    _bad({"max_tool_errors": 0}, other_path, "max_tool_errors")
    other_command = _run(
        *_call("c1", "run_command", {"argv": ["pytest"]}, error="fail"),
        *_call("c2", "run_command", {"argv": ["ruff"]}),
    )
    _bad({"max_tool_errors": 0}, other_command, "max_tool_errors")
    other_tool = _run(
        *_call("c1", "read_file", {"path": "a.txt"}, error="missing"),
        *_call("c2", "glob", {"path": "a.txt"}),
    )
    _bad({"max_tool_errors": 0}, other_tool, "max_tool_errors")


def test_workspace_assertions_share_one_implementation(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    workspace = tmp_path / "workspace"
    fixture.mkdir()
    workspace.mkdir()
    (fixture / "keep.txt").write_text("same", encoding="utf-8")
    (workspace / "keep.txt").write_text("same", encoding="utf-8")
    (workspace / "note.txt").write_text("hello needle", encoding="utf-8")
    run = RunView(workspace=workspace, fixture_dir=fixture)
    _ok(
        {
            "workspace_file_contains": {"note.txt": "needle"},
            "workspace_file_absent": ["gone.txt"],
            "workspace_unchanged": ["keep.txt"],
            "workspace_command_passes": "python -c \"print('ok')\"",
        },
        run,
    )
    _bad({"workspace_file_contains": {"note.txt": "missing"}}, run, "workspace_file_contains")
    (workspace / "gone.txt").write_text("x", encoding="utf-8")
    _bad({"workspace_file_absent": ["gone.txt"]}, run, "workspace_file_absent")
    (workspace / "keep.txt").write_text("changed", encoding="utf-8")
    _bad({"workspace_unchanged": ["keep.txt"]}, run, "workspace_unchanged")
    _bad(
        {"workspace_command_passes": 'python -c "raise SystemExit(3)"'},
        run,
        "workspace_command_passes",
    )
    bare = RunView()
    failures = evaluate(
        {
            "workspace_file_contains": {"note.txt": "needle"},
            "workspace_file_absent": ["gone.txt"],
            "workspace_unchanged": ["keep.txt"],
            "workspace_command_passes": ["python -c pass"],
        },
        bare,
    )
    assert failures == ["workspace_*: no workspace directory was provided for this run"]


def test_workspace_commands_use_one_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[object] = []

    def fake_run(*_args: object, **kwargs: object) -> object:
        seen.append(kwargs.get("timeout"))

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr("regression.schema.assertions.subprocess.run", fake_run)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _ok({"workspace_command_passes": ["python -c pass"]}, RunView(workspace=workspace))
    assert WORKSPACE_COMMAND_TIMEOUT_SEC == 180
    assert seen == [180]


def test_files_not_touched_matches_path_globs() -> None:
    run = _run(*_call("c1", "read_file", {"path": "src/app.py"}))
    _ok({"files_not_touched": ["secret/*"]}, run)
    _bad({"files_not_touched": ["src/*.py"]}, run, "files_not_touched")


def test_verified_before_final_pass_and_fail() -> None:
    verified = _run(*_call("c1", "run_command", {"argv": ["pytest", "-q"]}))
    _ok({"verified_before_final": True}, verified)
    wrote_after = _run(
        *_call("c1", "run_command", {"argv": ["pytest", "-q"]}),
        *_call("c2", "write_file", {"path": "a.txt"}),
    )
    _bad({"verified_before_final": True}, wrote_after, "verified_before_final")


def test_history_matchers() -> None:
    request = ToolRequest(id="c1", name="read_file", args={"path": "a.txt"})
    response = ToolResponse(id="c1", tool_name="read_file", result=[Text(text="ok")])
    rows = [
        Message(role="user", content=[Text(text="hi")], row_id="r1"),
        Message(role="assistant", content=[Text(text="[Context Summary]:\nolder")], row_id="r2"),
        Message(role="assistant", content=[request], row_id="r3"),
        Message(role="user", content=[response], row_id="r4"),
    ]
    run = RunView(history=rows)
    _ok(
        {
            "history": {
                "row_count": 4,
                "roles": ["user", "assistant", "assistant", "user"],
                "tool_pairs_intact": True,
                "summary_row": True,
                "row_ids_preserved": ["r1", "r3"],
                "no_repaired_rows": True,
            }
        },
        run,
    )
    orphan = RunView(history=[Message(role="assistant", content=[request], row_id="r3")])
    _bad({"history": {"tool_pairs_intact": True}}, orphan, "history")
    pinned = RunView(
        history=[
            Message(
                role="user",
                content=[
                    SystemNotification(notification_type="verifierVerdict", msg="verdict-kept")
                ],
            ),
            Message(
                role="assistant",
                content=[Thinking(thinking="tail-thought-pin"), Text(text="tail-ok")],
            ),
        ]
    )
    _ok(
        {
            "history": {
                "blocks_include": [
                    {
                        "type": "systemNotification",
                        "notification_type": "verifierVerdict",
                        "text_contains": "verdict-kept",
                    },
                    {"type": "thinking", "text_contains": "tail-thought-pin"},
                ]
            }
        },
        pinned,
    )
    _bad(
        {"history": {"blocks_include": [{"type": "thinking", "text_contains": "missing"}]}},
        pinned,
        "history",
    )
    repaired = ToolResponse(
        id="c1",
        tool_name="read_file",
        result=[Text(text=interrupted_tool_result_text("read_file"))],
        is_error=True,
    )
    _bad(
        {"history": {"no_repaired_rows": True}},
        RunView(
            history=[
                Message(role="assistant", content=[request]),
                Message(role="user", content=[repaired]),
            ]
        ),
        "history",
    )


def test_store_row_counts() -> None:
    run = RunView(store=StoreView(outbox=1, usage=2, loops=0, goals=1, branches=0))
    _ok({"store": {"outbox": 1, "usage": {"min": 1}, "loops": {"max": 0}, "goals": 1}}, run)
    _bad({"store": {"usage": {"max": 1}}}, run, "store")


def test_memory_injected_checks_content() -> None:
    run = RunView(memory_text="project-alpha is the main initiative")
    _ok({"memory_injected": {"contains": ["project-alpha", "initiative"]}}, run)
    _bad({"memory_injected": {"contains": "marigold"}}, run, "memory_injected")


def test_verdicts_matchers() -> None:
    run = _run(
        {
            "type": "VerifierVerdict",
            "status": "on_track",
            "severity": "nudge",
            "triggering_signals": ["goal_drift"],
        }
    )
    _ok(
        {
            "verdicts": {
                "min": 1,
                "max": 2,
                "status_in": ["on_track"],
                "severity_max": "nudge",
                "signals_include": ["goal_drift"],
                "signals_exclude": ["secret"],
            }
        },
        run,
    )
    drifting = _run(
        {
            "type": "VerifierVerdict",
            "status": "drifting",
            "severity": "replan",
            "triggering_signals": ["secret"],
        }
    )
    _bad({"verdicts": {"severity_max": "nudge"}}, drifting, "verdicts")


def test_subagent_matchers(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "progress.jsonl").write_text('{"n": 1}\n{"n": 2}\n', encoding="utf-8")
    (scratch / "output.json").write_text("{}", encoding="utf-8")
    (scratch / "notes.md").write_text("done", encoding="utf-8")
    run = RunView(
        subagent_dir=scratch,
        events=[
            {"type": "SubagentStarted"},
            {"type": "SubagentCompleted", "ok": True},
        ],
    )
    _ok(
        {
            "subagent": {
                "started": True,
                "completed_ok": True,
                "artifact_exists": "notes.md",
                "progress_jsonl_lines_min": 2,
                "output_json_present": True,
            }
        },
        run,
    )
    quiet = RunView(subagent_dir=tmp_path / "empty")
    _bad({"subagent": {"started": True}}, quiet, "subagent")


def test_sse_frame_expectations() -> None:
    frames = [
        {"type": "ActiveRequests", "request_id": "r1"},
        {"type": "AssistantDelta", "id": "1", "request_id": "r1", "delta": "hi"},
        {"type": "TurnComplete", "id": "2", "request_id": "r1"},
    ]
    run = RunView(sse_frames=frames, sse_replay={"2": ["3"]})
    _ok(
        {
            "sse": {
                "frames_have_ids": True,
                "active_requests_once": True,
                "one_turn_complete_per_request": True,
                "replay_from": {"last_event_id": "2", "event_ids": ["3"]},
            }
        },
        run,
    )
    missing_id = RunView(sse_frames=[{"type": "AssistantDelta", "request_id": "r1", "delta": "hi"}])
    _bad({"sse": {"frames_have_ids": True}}, missing_id, "sse")


def test_response_contains_is_or_and_all_is_and() -> None:
    run = _run({"type": "AssistantDelta", "delta": "The codename is marigold."})
    _ok({"response_contains": ["marigold", "absent phrase"]}, run)
    _bad({"response_contains": ["nope", "neither"]}, run, "response_contains")
    _ok({"response_contains_all": ["codename", "marigold"]}, run)
    _bad({"response_contains_all": ["marigold", "absent phrase"]}, run, "response_contains_all")


def test_response_not_contains_and_regexes() -> None:
    run = _run(
        {"type": "AssistantDelta", "delta": "draft"},
        {"type": "AssistantTextEnded", "text": "Total: $42.50"},
    )
    _ok({"response_not_contains": ["refuse"]}, run)
    _bad({"response_not_contains": ["total"]}, run, "response_not_contains")
    _ok({"response_regex": [r"\$\d+\.\d{2}", r"nope"]}, run)
    _bad({"response_regex": [r"^nope$"]}, run, "response_regex")
    _ok({"final_text_regex": [r"\$42\.50"]}, run)
    _bad({"final_text_regex": ["draft"]}, run, "final_text_regex")


def test_metrics_and_min_score() -> None:
    run = RunView(scores={"correctness": 0.9, "style": 0.8})
    _ok({"metrics": {"correctness": 0.8}}, run)
    _bad({"metrics": {"correctness": 0.95}}, run, "metrics")
    _ok({"min_score": 0.8}, run)
    _bad({"min_score": 0.85}, run, "min_score")


def test_live_budgets() -> None:
    run = _run(
        {
            "type": "TurnComplete",
            "usage": {
                "duration_ms": 50,
                "input_tokens": 10,
                "output_tokens": 4,
                "cost_usd": 0.01,
            },
        }
    )
    _ok(
        {
            "max_latency_ms": 100,
            "max_input_tokens": 20,
            "max_output_tokens": 8,
            "max_cost_usd": 0.02,
        },
        run,
    )
    _bad({"max_latency_ms": 10}, run, "max_latency_ms")
    _bad({"max_input_tokens": 5}, run, "max_input_tokens")
    _bad({"max_output_tokens": 1}, run, "max_output_tokens")
    _bad({"max_cost_usd": 0.001}, run, "max_cost_usd")


def test_list_assertions_stay_unparsed() -> None:
    assert evaluate(["turn_completed"], RunView()) == []


def test_wrong_mode_keys_are_rejected_at_load(tmp_path: Path) -> None:
    _reject(tmp_path, "live", "  events_sequence: [TurnComplete]\n", "events_sequence")
    _reject(tmp_path, "scripted", "  min_score: 0.5\n", "min_score")
    _reject(tmp_path, "live", "  interventions: [doom_loop]\n", "interventions")
    _reject(tmp_path, "live", "  provider_calls: 1\n", "provider_calls")
    _reject(tmp_path, "live", "  history:\n    row_count: 1\n", "history")


def test_sse_rejected_on_the_loop_driver_and_allowed_on_gateway(tmp_path: Path) -> None:
    _reject(tmp_path, "scripted", "  sse:\n    frames_have_ids: true\n", "sse")
    _load(
        tmp_path,
        "scripted",
        "  sse:\n    frames_have_ids: true\n",
        extra="driver: gateway\n",
    )
    _load(tmp_path, "live", "  sse:\n    frames_have_ids: true\n")


def test_unknown_assertion_key_is_rejected(tmp_path: Path) -> None:
    _reject(tmp_path, "scripted", "  max_turns: 4\n", "max_turns")


def test_memory_injected_bool_is_rejected(tmp_path: Path) -> None:
    _reject(tmp_path, "scripted", "  memory_injected: true\n", "contains")


def test_verifier_blocks_require_eval_verifier_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("EVAL_VERIFIER_MODE", raising=False)
    _reject(tmp_path, "live", "  verifier_on:\n    min_score: 0.2\n", "EVAL_VERIFIER_MODE")
    _reject(tmp_path, "live", "  verifier_off:\n    min_score: 0.2\n", "EVAL_VERIFIER_MODE")


def test_verifier_overlay_is_merged_when_the_mode_is_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = """\
case: T2-LOOP-07
mode: live
assertions:
  required_tools: [read_file]
  verifier_off:
    required_tools: [glob]
  verifier_on:
    required_tools: [write_file]
"""
    path = tmp_path / "case.yaml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("EVAL_VERIFIER_MODE", "on")
    scenario = load_scenario(path, root=tmp_path)
    write_run = _run(*_call("c1", "write_file", {"path": "a.txt"}))
    _ok(scenario.assertions, write_run)  # type: ignore[arg-type]
    monkeypatch.setenv("EVAL_VERIFIER_MODE", "off")
    glob_run = _run(*_call("c1", "glob", {"pattern": "*.py"}))
    _ok(scenario.assertions, glob_run)  # type: ignore[arg-type]
    _bad(scenario.assertions, write_run, "required_tools")  # type: ignore[arg-type]


def test_nested_verifier_key_in_the_wrong_mode_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EVAL_VERIFIER_MODE", "on")
    _reject(
        tmp_path,
        "live",
        "  verifier_on:\n    events_sequence: [TurnComplete]\n",
        "events_sequence",
    )


# Live-runner assertions (``regression/t4/assertions.py`` over ``EvalRun``). Moved in
# P4-01; the helpers carry an ``_eval_`` prefix so they do
# not shadow the ``RunView`` helpers above.


def _eval_scenario(assertions: dict) -> Scenario:
    return Scenario(id="s", messages=["hi"], assertions=assertions)


def _eval_run(
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
    run = _eval_run([ToolCallRecord(tool="read_file")])
    failures = evaluate_assertions(_eval_scenario({"required_tools": ["read_file"]}), run)
    assert failures == [], failures


def test_required_tools_missing() -> None:
    run = _eval_run([ToolCallRecord(tool="write_file")])
    failures = evaluate_assertions(_eval_scenario({"required_tools": ["read_file"]}), run)
    assert len(failures) == 1
    assert "required_tools" in failures[0]


def test_min_subagent_calls() -> None:
    run = _eval_run([ToolCallRecord(tool="task"), ToolCallRecord(tool="task")])
    ok = evaluate_assertions(_eval_scenario({"min_subagent_calls": 2}), run)
    assert ok == []
    bad = evaluate_assertions(_eval_scenario({"min_subagent_calls": 3}), run)
    assert len(bad) == 1


def test_max_tool_errors() -> None:
    run = _eval_run([ToolCallRecord(tool="read_file", error="boom")])
    ok = evaluate_assertions(_eval_scenario({"max_tool_errors": 1}), run)
    assert ok == []
    bad = evaluate_assertions(_eval_scenario({"max_tool_errors": 0}), run)
    assert len(bad) == 1


def test_tool_errors_count_self_corrected_same_tool_not_counted() -> None:
    run = _eval_run(
        [
            ToolCallRecord(tool="read_file", args_summary="bad/path.md", error="not found"),
            ToolCallRecord(tool="read_file", args_summary="good/path.md"),
        ]
    )
    assert run.tool_errors_count() == 0
    ok = evaluate_assertions(_eval_scenario({"max_tool_errors": 0}), run)
    assert ok == []


def test_tool_errors_count_unresolved_error_is_counted() -> None:
    run = _eval_run(
        [ToolCallRecord(tool="read_file", args_summary="bad/path.md", error="not found")]
    )
    assert run.tool_errors_count() == 1
    bad = evaluate_assertions(_eval_scenario({"max_tool_errors": 0}), run)
    assert len(bad) == 1


def test_tool_errors_count_ignores_success_on_a_different_tool() -> None:
    run = _eval_run(
        [
            ToolCallRecord(tool="read_file", args_summary="bad/path.md", error="not found"),
            ToolCallRecord(tool="glob", args_summary="**/*.md"),
        ]
    )
    assert run.tool_errors_count() == 1


def test_tool_errors_count_multi_error_mixed_resolution() -> None:
    # First read_file error is resolved by the later successful read_file call; the
    # write_file error has no later successful write_file call, so it stays unresolved.
    run = _eval_run(
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
    run = _eval_run(
        [
            ToolCallRecord(tool="write_file", args_summary="a.txt", error="disk full"),
            ToolCallRecord(tool="write_file", args_summary="b.txt"),
        ]
    )
    assert run.tool_errors_count() == 0


def test_max_latency_ms() -> None:
    run = _eval_run(duration_ms=5000)
    ok = evaluate_assertions(_eval_scenario({"max_latency_ms": 10000}), run)
    assert ok == []
    bad = evaluate_assertions(_eval_scenario({"max_latency_ms": 1000}), run)
    assert len(bad) == 1


def test_no_assertions_means_no_failures() -> None:
    run = _eval_run()
    assert evaluate_assertions(_eval_scenario({}), run) == []


def test_response_contains_pass_case_insensitive() -> None:
    run = _eval_run(output="The codename is Project-Marigold.")
    ok = evaluate_assertions(_eval_scenario({"response_contains": ["project-marigold"]}), run)
    assert ok == []


def test_response_contains_fail() -> None:
    run = _eval_run(output="I don't know.")
    bad = evaluate_assertions(_eval_scenario({"response_contains": ["project-marigold"]}), run)
    assert len(bad) == 1
    assert "response_contains" in bad[0]


def test_response_not_contains() -> None:
    run = _eval_run(output="As an AI, I cannot do that.")
    bad = evaluate_assertions(_eval_scenario({"response_not_contains": ["as an ai"]}), run)
    assert len(bad) == 1 and "response_not_contains" in bad[0]
    ok = evaluate_assertions(_eval_scenario({"response_not_contains": ["refuse"]}), run)
    assert ok == []


def test_response_regex() -> None:
    run = _eval_run(output="Total: $42.50 charged.")
    ok = evaluate_assertions(_eval_scenario({"response_regex": r"\$\d+\.\d{2}"}), run)
    assert ok == []
    bad = evaluate_assertions(_eval_scenario({"response_regex": [r"^\d+$"]}), run)
    assert len(bad) == 1 and "response_regex" in bad[0]


def test_response_regex_invalid_pattern_does_not_crash() -> None:
    run = _eval_run(output="Total: $42.50 charged.")
    bad = evaluate_assertions(
        _eval_scenario({"response_regex": [r"\$\d+\.\d{2}", r"(unclosed["]}), run
    )
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
    run = _eval_run()
    assert evaluate_assertions(_eval_scenario({"max_verdicts": 0}), run) == []


def test_max_verdicts_fail() -> None:
    run = _verdict_run(VerdictRecord(status="drifting", severity="nudge"))
    bad = evaluate_assertions(_eval_scenario({"max_verdicts": 0}), run)
    assert len(bad) == 1 and "max_verdicts" in bad[0]


def test_min_verdicts() -> None:
    run = _verdict_run(VerdictRecord(status="drifting", severity="none"))
    assert evaluate_assertions(_eval_scenario({"min_verdicts": 1}), run) == []
    bad = evaluate_assertions(_eval_scenario({"min_verdicts": 2}), run)
    assert len(bad) == 1 and "min_verdicts" in bad[0]


def test_verdict_status_in() -> None:
    run = _verdict_run(VerdictRecord(status="on_track", severity="none"))
    assert (
        evaluate_assertions(_eval_scenario({"verdict_status_in": ["on_track", "drifting"]}), run)
        == []
    )
    bad = evaluate_assertions(
        _eval_scenario({"verdict_status_in": ["on_track"]}),
        _verdict_run(VerdictRecord(status="stuck", severity="replan")),
    )
    assert len(bad) == 1 and "verdict_status_in" in bad[0]


def test_verdict_severity_max() -> None:
    run = _verdict_run(VerdictRecord(status="drifting", severity="nudge"))
    assert evaluate_assertions(_eval_scenario({"verdict_severity_max": "nudge"}), run) == []
    bad = evaluate_assertions(_eval_scenario({"verdict_severity_max": "none"}), run)
    assert len(bad) == 1 and "verdict_severity_max" in bad[0]


def test_files_not_touched() -> None:
    ok = _verdict_run(path_args=["src/app.py"])
    assert (
        evaluate_assertions(_eval_scenario({"files_not_touched": ["db/migrations/**"]}), ok) == []
    )
    bad = _verdict_run(path_args=["db/migrations/001.sql"])
    failures = evaluate_assertions(_eval_scenario({"files_not_touched": ["db/migrations/**"]}), bad)
    assert len(failures) == 1 and "files_not_touched" in failures[0]


def test_verifier_mode_nested_blocks() -> None:
    import os

    scenario = _eval_scenario(
        {
            "metrics": ["response_relevancy"],
            "verifier_off": {"max_verdicts": 0},
            "verifier_on": {"min_verdicts": 1},
        }
    )
    with_verdict = _verdict_run(VerdictRecord(status="drifting", severity="none"))
    empty = _eval_run()

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
    run = _eval_run(calls, output=output)
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
    failures = evaluate_assertions(
        _eval_scenario({"verified_before_final": True}), _eval_run(calls)
    )
    assert failures == [], failures


def test_verified_before_final_fails_without_any_verify() -> None:
    calls = [ToolCallRecord(tool="replace_in_file", args_summary="{}", ok=True)]
    bad = evaluate_assertions(_eval_scenario({"verified_before_final": True}), _eval_run(calls))
    assert len(bad) == 1 and "no verification command" in bad[0]


def test_verified_before_final_fails_when_last_verify_failed_or_unresolved() -> None:
    for ok in (False, None):
        bad = evaluate_assertions(
            _eval_scenario({"verified_before_final": True}), _eval_run([_pytest(True), _pytest(ok)])
        )
        assert len(bad) == 1 and "did not pass" in bad[0], ok


def test_verified_before_final_fails_when_write_follows_passing_verify() -> None:
    calls = [_pytest(True), ToolCallRecord(tool="write_file", args_summary="{}", ok=True)]
    bad = evaluate_assertions(_eval_scenario({"verified_before_final": True}), _eval_run(calls))
    assert len(bad) == 1 and "after the last passing verification" in bad[0]


def test_verified_before_final_custom_regex() -> None:
    cmd = ToolCallRecord(tool="run_command", args_summary='{"argv": ["./check.sh"]}', ok=True)
    scenario = _eval_scenario({"verified_before_final": "check\\.sh"})
    assert evaluate_assertions(scenario, _eval_run([cmd])) == []
    assert evaluate_assertions(_eval_scenario({"verified_before_final": True}), _eval_run([cmd]))


def test_verify_regex_matches_argv_list_and_command_forms() -> None:
    for summary in (
        '{"argv": ["npm", "test"]}',
        '{"command": "cargo test --all"}',
        '{"command": "make", "args": ["check"]}',
        '{"argv": ["uv", "run", "ruff", "check", "."]}',
    ):
        call = ToolCallRecord(tool="run_command", args_summary=summary, ok=True)
        scenario = _eval_scenario({"verified_before_final": True})
        assert evaluate_assertions(scenario, _eval_run([call])) == [], summary
    other = ToolCallRecord(tool="run_command", args_summary='{"argv": ["ls", "-la"]}', ok=True)
    assert evaluate_assertions(_eval_scenario({"verified_before_final": True}), _eval_run([other]))


def test_final_text_regex_checks_last_turn_only() -> None:
    first = TurnResult(input="a", output="calc/ops.py:12", usage=UsageSummary())
    last = TurnResult(input="b", output="nothing useful", usage=UsageSummary())
    run = EvalRun(run_id="r", scenario_id="s", turns=[first, last])
    bad = evaluate_assertions(_eval_scenario({"final_text_regex": r"calc/ops\.py:\d+"}), run)
    assert len(bad) == 1 and "final_text_regex" in bad[0]
    run.turns[-1].output = "defined at calc/ops.py:17"
    assert evaluate_assertions(_eval_scenario({"final_text_regex": r"calc/ops\.py:\d+"}), run) == []


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
            workspace_fixture="calc_repo",
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

    fixture = FIXTURES_ROOT / "calc_repo"
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "copy"
        shutil.copytree(fixture, ws)
        scenario = Scenario(
            id="c",
            messages=["hi"],
            workspace_fixture="calc_repo",
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

    fixture = FIXTURES_ROOT / "calc_repo_green"
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
    scenario = _eval_scenario({"workspace_command_passes": "true"})
    bad = evaluate_assertions(scenario, _eval_run())
    assert len(bad) == 1 and "no workspace directory" in bad[0]


def test_fixture_calc_repo_starts_with_exactly_one_failing_test() -> None:
    import shutil
    import subprocess
    import tempfile

    fixture = FIXTURES_ROOT / "calc_repo"
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
