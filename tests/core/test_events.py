"""Tests for :mod:`monkeybot.core.runtime.events`."""

from __future__ import annotations

import json
from typing import Literal, cast

import pytest
from monkeybot.core.runtime.events import (
    ActionRequiredEvent,
    AssistantDelta,
    Error,
    EventDecodeError,
    FrontendToolRequestEvent,
    ImageBlock,
    RedactedThinkingBlock,
    SystemNotificationEvent,
    Thinking,
    ThinkingBlockComplete,
    ThinkingBlockDelta,
    ToolCallResult,
    ToolCallStarted,
    ToolConfirmationRequestEvent,
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
    from monkeybot.core.runtime.events import ContextSummarizing

    ev = ContextSummarizing(
        request_id="r1", estimated_tokens=170_000, context_window_tokens=200_000
    )
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_context_summarized() -> None:
    from monkeybot.core.runtime.events import ContextSummarized

    ev = ContextSummarized(request_id="r1", turns_summarized=12)
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_system_prompt_snapshot() -> None:
    from monkeybot.core.runtime.events import SystemPromptSnapshot

    ev = SystemPromptSnapshot(request_id="r1", inner_turn=2, text="## Agent\n\nHello")
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


def test_sse_image_block_roundtrip() -> None:
    ev = ImageBlock(request_id="r", mime_type="image/png", data="abc")
    assert event_from_json(event_to_json(ev)) == ev


@pytest.mark.parametrize("signature", (None, "sig"))
def test_sse_thinking_block_delta_roundtrip(signature: str | None) -> None:
    ev = ThinkingBlockDelta(request_id="r", text="t", signature=signature)
    assert event_from_json(event_to_json(ev)) == ev


def test_sse_thinking_block_complete_roundtrip() -> None:
    ev = ThinkingBlockComplete(request_id="r", signature="anthropic-signature-or-empty")
    assert event_from_json(event_to_json(ev)) == ev


def test_sse_redacted_thinking_block_roundtrip() -> None:
    ev = RedactedThinkingBlock(request_id="r", data="opaque")
    assert event_from_json(event_to_json(ev)) == ev


@pytest.mark.parametrize("prompt", (None, "please confirm"))
def test_sse_tool_confirmation_request_event_roundtrip(prompt: str | None) -> None:
    ev = ToolConfirmationRequestEvent(
        request_id="r",
        tool_call_id="tc",
        tool_name="run_command",
        arguments={"x": 1},
        prompt=prompt,
    )
    assert event_from_json(event_to_json(ev)) == ev


@pytest.mark.parametrize(
    "action_type",
    ("elicitation", "toolConfirmation", "elicitationResponse"),
)
def test_sse_action_required_event_roundtrip(action_type: str) -> None:
    at = cast(Literal["elicitation", "toolConfirmation", "elicitationResponse"], action_type)
    ev = ActionRequiredEvent(
        request_id="r",
        action_type=at,
        id="e1",
        payload={"k": "v"},
    )
    assert event_from_json(event_to_json(ev)) == ev


def test_sse_frontend_tool_request_event_roundtrip() -> None:
    ev = FrontendToolRequestEvent(
        request_id="r",
        tool_call_id="f1",
        name="echo",
        args={"q": "hi"},
    )
    assert event_from_json(event_to_json(ev)) == ev


@pytest.mark.parametrize("data", (None, {"x": 1}))
def test_sse_system_notification_event_roundtrip(data: dict[str, object] | None) -> None:
    ev = SystemNotificationEvent(
        request_id="r",
        notification_type="creditsExhausted",
        msg="out of credits",
        data=data,
    )
    assert event_from_json(event_to_json(ev)) == ev
