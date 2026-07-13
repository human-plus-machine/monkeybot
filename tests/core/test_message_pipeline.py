"""Tests for transform_context / convert_to_provider pipeline."""

from __future__ import annotations

from monkeybot.core.llm.provider import Message
from monkeybot.core.messages import convert_to_provider, transform_context
from monkeybot.core.types.content_blocks import (
    AttachmentDescriptor,
    SystemNotification,
    Text,
    ToolRequest,
    ToolResponse,
)


def test_transform_strips_ui_only_blocks() -> None:
    msgs = [
        Message(
            role="user",
            content=[
                Text(text="hi"),
                AttachmentDescriptor(
                    attachment_id="a1",
                    mime_type="image/png",
                    description="shot",
                ),
                SystemNotification(notification_type="inlineMessage", msg="note"),
            ],
        )
    ]
    out = transform_context(msgs)
    assert len(out) == 1
    assert len(out[0].content) == 1
    assert isinstance(out[0].content[0], Text)
    assert out[0].content[0].text == "hi"


def test_transform_drops_ui_only_turns() -> None:
    msgs = [
        Message(
            role="assistant",
            content=[
                SystemNotification(notification_type="thinkingMessage", msg="…"),
            ],
        ),
        Message(role="user", content=[Text(text="ok")]),
    ]
    out = transform_context(msgs)
    assert len(out) == 1
    assert out[0].role == "user"


def test_transform_repairs_missing_tool_result() -> None:
    msgs = [
        Message(
            role="assistant",
            content=[ToolRequest(id="c1", name="read_file", args={"path": "a"})],
        ),
        # Missing tool response — integrity repair should synthesize one.
        Message(role="user", content=[Text(text="continue")]),
    ]
    out = transform_context(msgs)
    # Repair inserts a synthetic tool-result user turn before the next user text.
    tool_results = [
        b
        for m in out
        for b in m.content
        if isinstance(b, ToolResponse)
    ]
    assert any(b.id == "c1" for b in tool_results)


def test_convert_to_provider_passthrough_without_store() -> None:
    msgs = [Message(role="user", content=[Text(text="x")])]
    out = convert_to_provider(msgs, attachment_store=None, session_id="s1")
    assert out == msgs
