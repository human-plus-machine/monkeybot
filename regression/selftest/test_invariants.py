"""§5.3.2 global invariants: one positive and one negative each."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monkeybot.core.llm.provider import Message
from monkeybot.core.runtime.events import HarnessIntervention, TurnComplete
from monkeybot.core.types.content_blocks import Text, ToolRequest, ToolResponse
from regression.schema.assertions import RunView
from regression.schema.invariants import SSE_SCHEMA_PATH, check_invariants
from regression.schema.scenario import InvariantSkip


def _schema(tmp_path: Path, body: dict[str, object] | None = None) -> Path:
    path = tmp_path / "schema.json"
    document = body or {
        "type": "object",
        "required": ["type"],
        "properties": {"type": {"type": "string"}, "request_id": {"type": "string"}},
        "additionalProperties": True,
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _loop(*events: object, **kwargs: object) -> RunView:
    payload = list(events) if events else [{"type": "TurnComplete"}]
    return RunView(events=payload, driver="loop", **kwargs)  # type: ignore[arg-type]


def _only(run: RunView, token: str, *, schema_path: Path | None = None) -> None:
    failures = check_invariants(run, schema_path=schema_path)
    assert len(failures) == 1, failures
    assert token in failures[0]


def _clean(run: RunView, *, schema_path: Path | None = None) -> None:
    failures = check_invariants(run, schema_path=schema_path)
    assert failures == [], failures


def test_turn_complete_is_exactly_one_and_last() -> None:
    _clean(_loop({"type": "AssistantDelta", "delta": "hi"}, {"type": "TurnComplete"}))
    _clean(
        _loop(
            {"type": "TurnComplete", "request_id": "r1"},
            {"type": "AssistantDelta", "delta": "b", "request_id": "r2"},
            {"type": "TurnComplete", "request_id": "r2"},
        )
    )
    _only(_loop({"type": "TurnComplete"}, {"type": "Error", "error": "after"}), "turn_complete")
    _only(_loop({"type": "AssistantDelta", "delta": "hi"}), "turn_complete")
    _only(
        _loop({"type": "TurnComplete"}, {"type": "TurnComplete"}),
        "turn_complete",
    )
    _only(
        _loop(
            {"type": "TurnComplete", "request_id": "r1"},
            {"type": "TurnComplete", "request_id": "r1"},
        ),
        "turn_complete",
    )


def test_tool_call_results_pair_on_call_id() -> None:
    _clean(
        _loop(
            {"type": "ToolCallStarted", "tool": "read_file", "call_id": ""},
            {"type": "ToolCallStarted", "tool": "read_file", "call_id": "c1"},
            {"type": "ToolCallResult", "tool": "read_file", "call_id": "c1"},
            {"type": "TurnComplete"},
        )
    )
    _only(
        _loop(
            {"type": "ToolCallStarted", "tool": "read_file", "call_id": "c1"},
            {"type": "TurnComplete"},
        ),
        "tool_call_results",
    )
    _only(
        _loop(
            {"type": "ToolCallStarted", "tool": "read_file", "call_id": "c1"},
            {"type": "ToolCallResult", "tool": "read_file", "call_id": "c1"},
            {"type": "ToolCallResult", "tool": "read_file", "call_id": "c1"},
            {"type": "TurnComplete"},
        ),
        "tool_call_results",
    )


def test_no_tool_after_cancel() -> None:
    _clean(
        _loop(
            {"type": "ToolCallStarted", "tool": "read_file", "call_id": "c1", "args": {}},
            {"type": "ToolCallResult", "tool": "read_file", "call_id": "c1"},
            {"type": "Error", "error": "Request cancelled"},
            {"type": "TurnComplete"},
        )
    )
    _only(
        _loop(
            {"type": "Error", "error": "Request cancelled"},
            {"type": "ToolCallStarted", "tool": "read_file", "call_id": "c1"},
            {"type": "ToolCallResult", "tool": "read_file", "call_id": "c1"},
            {"type": "TurnComplete"},
        ),
        "no_tool_after_cancel",
    )


def test_history_has_no_orphan_tool_pairs() -> None:
    request = ToolRequest(id="c1", name="read_file", args={"path": "a.txt"})
    response = ToolResponse(id="c1", tool_name="read_file", result=[Text(text="ok")])
    paired = [
        Message(role="assistant", content=[request]),
        Message(role="user", content=[response]),
    ]
    _clean(_loop(history=paired))
    _only(
        _loop(history=[Message(role="assistant", content=[request])]),
        "history_pairs",
    )
    _only(
        _loop(history=[Message(role="user", content=[response])]),
        "history_pairs",
    )


def test_no_harness_intervention_on_the_sse_wire(tmp_path: Path) -> None:
    schema = _schema(tmp_path)
    transcript = [
        {"type": "HarnessIntervention", "intervention": "doom_loop"},
        {"type": "TurnComplete"},
    ]
    _clean(
        RunView(
            driver="gateway",
            events=transcript,
            sse_frames=[{"type": "TurnComplete", "id": "1"}],
            serialized_events=[{"type": "TurnComplete"}],
            second_reply_status=200,
        ),
        schema_path=schema,
    )
    _only(
        RunView(
            driver="gateway",
            events=[{"type": "TurnComplete"}],
            sse_frames=[{"type": "HarnessIntervention", "intervention": "doom_loop"}],
            serialized_events=[{"type": "TurnComplete"}],
            second_reply_status=200,
        ),
        "no_sse_intervention",
        schema_path=schema,
    )
    # The transcript event is allowed; the wire check is gateway-only.
    _clean(_loop(HarnessIntervention(intervention="doom_loop"), TurnComplete()))


def test_sse_schema_validates_serialized_events(tmp_path: Path) -> None:
    schema = _schema(tmp_path)
    _clean(
        RunView(driver="gateway", events=[TurnComplete()], second_reply_status=200),
        schema_path=schema,
    )
    rejecting = _schema(
        tmp_path,
        {
            "type": "object",
            "required": ["type"],
            "properties": {"type": {"const": "Nope"}},
            "additionalProperties": True,
        },
    )
    _only(
        RunView(driver="gateway", events=[TurnComplete()], second_reply_status=200),
        "sse_schema",
        schema_path=rejecting,
    )


def test_sse_schema_missing_file_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    _only(
        RunView(driver="gateway", events=[TurnComplete()], second_reply_status=200),
        "not found",
        schema_path=missing,
    )
    assert SSE_SCHEMA_PATH.name == "schema.json"


def test_strict_script_consumes_every_call_unless_partial() -> None:
    _clean(_loop(scripted=True, script_remaining=0, script_partial=False))
    _clean(_loop(scripted=True, script_remaining=2, script_partial=True))
    _clean(_loop(scripted=False, script_remaining=4))
    _only(
        _loop(scripted=True, script_remaining=2, script_partial=False),
        "strict_script",
    )


def test_turn_lock_is_released(tmp_path: Path) -> None:
    schema = _schema(tmp_path)
    base = {
        "driver": "gateway",
        "events": [{"type": "TurnComplete"}],
        "serialized_events": [{"type": "TurnComplete"}],
    }
    _clean(RunView(second_reply_status=200, **base), schema_path=schema)
    _only(RunView(second_reply_status=409, **base), "turn_lock", schema_path=schema)
    _clean(_loop(second_reply_status=409))


def test_invariants_skip_requires_a_reason_and_is_honored() -> None:
    doubled = _loop({"type": "TurnComplete"}, {"type": "TurnComplete"})
    _only(doubled, "turn_complete")
    skipped = check_invariants(
        doubled,
        [InvariantSkip(name="turn_complete", reason="this case ends on an error after the turn")],
    )
    assert skipped == []
    with pytest.raises(ValueError, match="reason"):
        check_invariants(doubled, [{"name": "turn_complete"}])
    with pytest.raises(ValueError, match="unknown invariant"):
        check_invariants(doubled, [{"name": "not_an_invariant", "reason": "typo"}])
