"""Tests for :mod:`monkeybot.core.events`."""

from __future__ import annotations

import json

import pytest
from monkeybot.core.events import (
    AssistantDelta,
    Error,
    EventDecodeError,
    Thinking,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
    UsageTotals,
    event_from_json,
    event_to_json,
)


def test_agent_event_roundtrip_thinking() -> None:
    ev = Thinking(request_id="r1")
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_assistant_delta() -> None:
    ev = AssistantDelta(request_id="r1", delta="café")
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_tool_call_started() -> None:
    ev = ToolCallStarted(
        request_id="r1",
        tool="run_command",
        label="Run",
        args={"cmd": "ls"},
    )
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_tool_call_result_no_error() -> None:
    ev = ToolCallResult(request_id="r1", tool="t", result="ok", error=None)
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_tool_call_result_with_error() -> None:
    ev = ToolCallResult(request_id="r1", tool="t", result="partial", error="x")
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_turn_complete() -> None:
    ev = TurnComplete(request_id="r1", usage=UsageTotals(1, 2, 3, 0.5, 99))
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_error() -> None:
    ev = Error(request_id="r1", error="boom")
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_context_summarizing() -> None:
    from monkeybot.core.events import ContextSummarizing

    ev = ContextSummarizing(
        request_id="r1", estimated_tokens=170_000, context_window_tokens=200_000
    )
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_context_summarized() -> None:
    from monkeybot.core.events import ContextSummarized

    ev = ContextSummarized(request_id="r1", turns_summarized=12)
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_turn_complete_numeric_cost_usd_int() -> None:
    payload = '{"type":"TurnComplete","request_id":"r","usage":{"cost_usd":0}}'
    out = event_from_json(payload)
    assert isinstance(out, TurnComplete)
    assert out.usage.cost_usd == 0.0


def test_event_from_json_rejects_invalid_json() -> None:
    raw = "{not json"
    with pytest.raises(EventDecodeError, match="invalid JSON"):
        event_from_json(raw)


def test_event_from_json_rejects_missing_type() -> None:
    with pytest.raises(EventDecodeError, match="missing type"):
        event_from_json("{}")


def test_event_from_json_rejects_unknown_type() -> None:
    raw = '{"type":"Nope","request_id":"x"}'
    with pytest.raises(EventDecodeError, match="unknown"):
        event_from_json(raw)


def test_event_from_json_accepts_kind_alias() -> None:
    raw = json.dumps({"kind": "Thinking", "request_id": "rid"}, separators=(",", ":"))
    out = event_from_json(raw)
    assert out == Thinking(request_id="rid")


def test_tool_call_started_args_default_empty_object() -> None:
    payload = '{"type":"ToolCallStarted","request_id":"r1","tool":"x","label":"L"}'
    out = event_from_json(payload)
    assert isinstance(out, ToolCallStarted)
    assert out.args == {}
