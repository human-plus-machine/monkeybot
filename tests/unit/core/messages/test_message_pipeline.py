"""Tests for transform_context / convert_to_provider pipeline."""

from __future__ import annotations

import pytest

from monkeybot.core.llm.provider import Message
from monkeybot.core.messages import convert_to_provider, transform_context
from monkeybot.core.types.content_blocks import (
    ActionRequired,
    AttachmentDescriptor,
    FrontendToolRequest,
    SystemNotification,
    Text,
    Thinking,
    ToolConfirmationAction,
    ToolConfirmationRequest,
    ToolRequest,
    ToolResponse,
)
from monkeybot.providers._bedrock_converse import messages_to_converse


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
    tool_results = [b for m in out for b in m.content if isinstance(b, ToolResponse)]
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


def _tool_pair_with_gap(gap: Message) -> list[Message]:
    return [
        Message(role="user", content=[Text(text="hi")]),
        Message(role="assistant", content=[ToolRequest(id="t1", name="run_command", args={})]),
        gap,
        Message(
            role="user",
            content=[
                ToolResponse(id="t1", tool_name="run_command", result=[Text(text="ok")]),
            ],
        ),
    ]


def _tool_result_ids(messages: list[Message]) -> list[str]:
    return [
        block.id
        for message in messages
        for block in message.content
        if isinstance(block, ToolResponse)
    ]


@pytest.mark.parametrize(
    "gap",
    [
        Message(
            role="assistant",
            content=[
                ToolConfirmationRequest(id="t1", tool_name="run_command", arguments={}),
            ],
        ),
        Message(
            role="user",
            content=[SystemNotification(notification_type="inlineMessage", msg="approved")],
        ),
        Message(
            role="system",
            content=[SystemNotification(notification_type="verifierVerdict", msg="nudge")],
        ),
        Message(
            role="assistant",
            content=[FrontendToolRequest(id="t1", name="run_command", args={})],
        ),
        Message(
            role="user",
            content=[
                ActionRequired(
                    data=ToolConfirmationAction(
                        id="t1",
                        tool_name="run_command",
                        arguments={},
                    )
                )
            ],
        ),
        Message(
            role="user",
            content=[
                AttachmentDescriptor(
                    attachment_id="a1",
                    mime_type="image/png",
                    description="shot",
                )
            ],
        ),
    ],
)
def test_transform_keeps_one_tool_result_when_ui_row_splits_the_pair(gap: Message) -> None:
    """A UI-only row between a call and its result must not invent a second result.

    Bedrock rejects a user turn whose toolResult count exceeds the previous
    turn's toolUse count. The real result is the one that survives.
    """
    out = transform_context(_tool_pair_with_gap(gap))
    results = [
        block for message in out for block in message.content if isinstance(block, ToolResponse)
    ]
    assert [block.id for block in results] == ["t1"]
    assert results[0].is_error is False
    assert isinstance(results[0].result[0], Text)
    assert results[0].result[0].text == "ok"
    _, converse = messages_to_converse(out)
    tool_results = [block for block in converse[-1]["content"] if "toolResult" in block]
    tool_uses = [
        block["toolUse"]["toolUseId"]
        for message in converse
        for block in message["content"]
        if "toolUse" in block
    ]
    assert tool_uses == ["t1"]
    assert len(tool_results) == 1
    assert tool_results[0]["toolResult"]["toolUseId"] == "t1"
    assert "status" not in tool_results[0]["toolResult"]


def test_transform_keeps_one_result_per_call_when_ui_row_splits_a_batch() -> None:
    out = transform_context(
        [
            Message(role="user", content=[Text(text="hi")]),
            Message(
                role="assistant",
                content=[
                    ToolRequest(id="t1", name="run_command", args={}),
                    ToolRequest(id="t2", name="read_file", args={}),
                ],
            ),
            Message(
                role="assistant",
                content=[
                    ToolConfirmationRequest(id="t1", tool_name="run_command", arguments={}),
                ],
            ),
            Message(
                role="user",
                content=[
                    ToolResponse(id="t1", tool_name="run_command", result=[Text(text="ok")]),
                    ToolResponse(id="t2", tool_name="read_file", result=[Text(text="file")]),
                ],
            ),
        ]
    )
    assert _tool_result_ids(out) == ["t1", "t2"]


def test_convert_to_provider_passthrough_without_store() -> None:
    msgs = [Message(role="user", content=[Text(text="x")])]
    out = convert_to_provider(msgs, attachment_store=None, session_id="s1")
    assert out == msgs
