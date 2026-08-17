"""Tests for transform_context / convert_to_provider pipeline."""

from __future__ import annotations

from monkeybot.core.llm.provider import Message
from monkeybot.core.messages import convert_to_provider, transform_context
from monkeybot.core.types.content_blocks import (
    AttachmentDescriptor,
    SystemNotification,
    Text,
    Thinking,
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


def test_transform_coalesces_roles_after_dropping_interior_ui_only_turn() -> None:
    """Dropping an interior UI-only row must not leave adjacent equal roles.

    ``user → assistant → user(UI-only) → assistant`` becomes two adjacent
    assistant messages after the strip; Anthropic/Gemini reject that shape.
    """
    msgs = [
        Message(role="user", content=[Text(text="hi")]),
        Message(role="assistant", content=[Text(text="first")]),
        Message(
            role="user",
            content=[
                SystemNotification(notification_type="inlineMessage", msg="ui only"),
            ],
        ),
        Message(role="assistant", content=[Text(text="second")]),
    ]
    out = transform_context(msgs)
    roles = [m.role for m in out]
    assert roles == ["user", "assistant"]
    assert all(a != b for a, b in zip(roles, roles[1:]))
    assert [b.text for b in out[1].content if isinstance(b, Text)] == ["first", "second"]


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


def test_transform_strips_thinking_from_final_assistant_turns() -> None:
    """Final-text Thinking is UI/history-only; keep it on tool-request turns."""
    msgs = [
        Message(role="user", content=[Text(text="hi")]),
        Message(
            role="assistant",
            content=[
                Thinking(thinking=" prior ", signature="sig-final"),
                Text(text="hello"),
            ],
        ),
        Message(role="user", content=[Text(text="use a tool")]),
        Message(
            role="assistant",
            content=[
                Thinking(thinking=" need tool ", signature="sig-tool"),
                ToolRequest(id="c1", name="run_command", args={"command": "ls"}),
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResponse(
                    id="c1",
                    tool_name="run_command",
                    result=[Text(text="ok")],
                )
            ],
        ),
    ]
    out = transform_context(msgs)
    assert [m.role for m in out] == ["user", "assistant", "user", "assistant", "user"]
    final = out[1]
    assert all(not isinstance(b, Thinking) for b in final.content)
    assert [b.text for b in final.content if isinstance(b, Text)] == ["hello"]
    tool_turn = out[3]
    thinking = [b for b in tool_turn.content if isinstance(b, Thinking)]
    assert len(thinking) == 1
    assert thinking[0].thinking == " need tool "
    assert thinking[0].signature == "sig-tool"


def test_convert_to_provider_passthrough_without_store() -> None:
    msgs = [Message(role="user", content=[Text(text="x")])]
    out = convert_to_provider(msgs, attachment_store=None, session_id="s1")
    assert out == msgs
