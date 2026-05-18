"""Serde and failure-mode tests for typed ContentBlock union and Message.

Variant checklist (paired tests in this module): Text, Image, ToolRequest,
ToolResponse, ToolConfirmationRequest, ActionRequired, FrontendToolRequest,
Thinking, RedactedThinking, SystemNotification.
"""

from __future__ import annotations

import json

import pytest

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import (
    ActionRequired,
    ContentBlock,
    ElicitationAction,
    ElicitationResponseAction,
    FrontendToolRequest,
    Image,
    RedactedThinking,
    SystemNotification,
    SystemNotificationType,
    Text,
    Thinking,
    ToolConfirmationAction,
    ToolConfirmationRequest,
    ToolRequest,
    ToolResponse,
)


def test_text_roundtrip() -> None:
    original = Text(text="hi")
    assert Text.from_dict(original.to_dict()) == original


def test_image_roundtrip() -> None:
    original = Image(mime_type="image/png", data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB")
    assert Image.from_dict(original.to_dict()) == original


@pytest.mark.parametrize("parse_error", [None, "oops"])
def test_tool_request_roundtrip(parse_error: str | None) -> None:
    original = ToolRequest(id="c1", name="echo", args={"x": 1}, parse_error=parse_error)
    dumped = original.to_dict()
    if parse_error is None:
        assert "parseError" not in dumped
    assert ToolRequest.from_dict(dumped) == original


def test_tool_response_roundtrip_nested() -> None:
    original = ToolResponse(
        id="c1",
        tool_name="echo",
        result=[
            Text(text="nested"),
            Image(mime_type="image/jpeg", data="YWJj"),
        ],
    )
    assert ToolResponse.from_dict(original.to_dict()) == original


@pytest.mark.parametrize("prompt", [None, "Approve this dangerous operation?"])
def test_tool_confirmation_request_roundtrip(prompt: str | None) -> None:
    original = ToolConfirmationRequest(
        id="conf1",
        tool_name="run_command",
        arguments={"cmd": "ls"},
        prompt=prompt,
    )
    dumped = original.to_dict()
    if prompt is None:
        assert "prompt" not in dumped
    assert ToolConfirmationRequest.from_dict(dumped) == original


def test_frontend_tool_request_roundtrip() -> None:
    original = FrontendToolRequest(
        id="f1",
        name="ui_echo",
        args={"k": "v"},
        parse_error=None,
    )
    dumped = original.to_dict()
    assert dumped["type"] == "frontendToolRequest"
    assert FrontendToolRequest.from_dict(dumped) == original


@pytest.mark.parametrize("signature", ["", "non-empty-sig"])
def test_thinking_roundtrip(signature: str) -> None:
    original = Thinking(thinking="step-wise reasoning", signature=signature)
    assert Thinking.from_dict(original.to_dict()) == original


def test_redacted_thinking_roundtrip() -> None:
    original = RedactedThinking(data="b64opaque")
    assert RedactedThinking.from_dict(original.to_dict()) == original


@pytest.mark.parametrize(
    "notification_type",
    ["thinkingMessage", "inlineMessage", "creditsExhausted"],
)
def test_system_notification_roundtrip(notification_type: SystemNotificationType) -> None:
    original = SystemNotification(notification_type=notification_type, msg="hello", data=None)
    dumped = original.to_dict()
    assert "data" not in dumped
    assert SystemNotification.from_dict(dumped) == original


@pytest.mark.parametrize(
    "action_data",
    [
        ToolConfirmationAction(
            id="tc1",
            tool_name="delete_files",
            arguments={"path": "/tmp"},
            prompt=None,
        ),
        ElicitationAction(
            id="el1",
            message="Please answer",
            requested_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        ),
        ElicitationResponseAction(id="elr1", user_data={"choice": True}),
    ],
)
def test_action_required_roundtrip_parametrized(
    action_data: ToolConfirmationAction | ElicitationAction | ElicitationResponseAction,
) -> None:
    original = ActionRequired(data=action_data)
    assert ActionRequired.from_dict(original.to_dict()) == original


def test_message_mixed_blocks_json_roundtrip() -> None:
    m = Message(
        role="assistant",
        content=[
            Text(text="intro"),
            ToolRequest(id="t-a", name="first", args={"a": 1}),
            ToolRequest(id="t-b", name="second", args={"b": 2}),
            Thinking(thinking="planning", signature=""),
        ],
    )
    round_tripped = Message.from_dict(json.loads(json.dumps(m.to_dict())))
    assert round_tripped == m


def test_message_empty_content_roundtrip() -> None:
    m = Message(role="assistant", content=[])
    assert Message.from_dict(m.to_dict()) == m


def test_content_block_unknown_type_raises() -> None:
    with pytest.raises(ValueError, match="unknown ContentBlock type"):
        ContentBlock.from_dict({"type": "futureBlock", "foo": 1})


def test_text_missing_required_field_raises() -> None:
    with pytest.raises(ValueError):
        ContentBlock.from_dict({"type": "text"})


def test_message_constructor_rejects_tool_role_runtime() -> None:
    with pytest.raises(ValueError, match="invalid role"):
        Message(role="tool", content=[])  # type: ignore[arg-type]


def test_message_from_dict_rejects_tool_role() -> None:
    with pytest.raises(ValueError, match="invalid role"):
        Message.from_dict({"role": "tool", "content": []})


def test_action_required_unknown_action_type_raises() -> None:
    payload: dict[str, object] = {
        "type": "actionRequired",
        "data": {"actionType": "notARealAction", "id": "x"},
    }
    with pytest.raises(ValueError):
        ContentBlock.from_dict(payload)
