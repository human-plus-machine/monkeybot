"""Wire-shape assertions for Story 5 SSE events (design 1B §4.2)."""

from __future__ import annotations

import json

from monkeybot.core.events import (
    ActionRequiredEvent,
    FrontendToolRequestEvent,
    ImageBlock,
    RedactedThinkingBlock,
    SystemNotificationEvent,
    ThinkingBlockComplete,
    ThinkingBlockDelta,
    ToolConfirmationRequestEvent,
    event_to_json,
)


def test_sse_wire_shape_image_block() -> None:
    ev = ImageBlock(request_id="r", mime_type="image/png", data="abc")
    d = json.loads(event_to_json(ev))
    assert d["type"] == "ImageBlock"
    assert set(d.keys()) >= {"type", "request_id", "mime_type", "data"}


def test_sse_wire_shape_thinking_block_delta() -> None:
    ev = ThinkingBlockDelta(request_id="r", text="frag", signature=None)
    d = json.loads(event_to_json(ev))
    assert d["type"] == "ThinkingBlockDelta"
    assert set(d.keys()) >= {"type", "request_id", "text", "signature"}


def test_sse_wire_shape_thinking_block_complete() -> None:
    ev = ThinkingBlockComplete(request_id="r", signature="sig")
    d = json.loads(event_to_json(ev))
    assert d["type"] == "ThinkingBlockComplete"
    assert set(d.keys()) >= {"type", "request_id", "signature"}


def test_sse_wire_shape_redacted_thinking_block() -> None:
    ev = RedactedThinkingBlock(request_id="r", data="opaque")
    d = json.loads(event_to_json(ev))
    assert d["type"] == "RedactedThinkingBlock"
    assert set(d.keys()) >= {"type", "request_id", "data"}


def test_sse_wire_shape_tool_confirmation_request() -> None:
    ev = ToolConfirmationRequestEvent(
        request_id="r",
        tool_call_id="c1",
        tool_name="run_command",
        arguments={"cmd": "ls"},
        prompt="ok?",
    )
    d = json.loads(event_to_json(ev))
    assert d["type"] == "ToolConfirmationRequest"
    assert set(d.keys()) >= {
        "type",
        "request_id",
        "tool_call_id",
        "tool_name",
        "arguments",
        "prompt",
    }


def test_sse_wire_shape_action_required_event() -> None:
    ev = ActionRequiredEvent(
        request_id="r",
        action_type="elicitation",
        id="e1",
        payload={"message": "m"},
    )
    d = json.loads(event_to_json(ev))
    assert d["type"] == "ActionRequiredEvent"
    assert set(d.keys()) >= {"type", "request_id", "action_type", "id", "payload"}


def test_sse_wire_shape_frontend_tool_request() -> None:
    ev = FrontendToolRequestEvent(
        request_id="r",
        tool_call_id="f1",
        name="echo",
        args={"x": 1},
    )
    d = json.loads(event_to_json(ev))
    assert d["type"] == "FrontendToolRequest"
    assert set(d.keys()) >= {"type", "request_id", "tool_call_id", "name", "args"}


def test_sse_wire_shape_system_notification_event() -> None:
    ev = SystemNotificationEvent(
        request_id="r",
        notification_type="creditsExhausted",
        msg="done",
        data=None,
    )
    d = json.loads(event_to_json(ev))
    assert d["type"] == "SystemNotificationEvent"
    assert set(d.keys()) >= {"type", "request_id", "notification_type", "msg", "data"}
