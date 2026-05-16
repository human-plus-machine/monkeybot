"""Shared typed message fixtures for provider adapter tests (Story 3)."""

from __future__ import annotations

from monkeybot.core.types.content_blocks import Text, ToolRequest, ToolResponse
from monkeybot.core.llm.provider import Message
from monkeybot.core.types.types_tools import ToolDef

CANONICAL_TOOL_DEFS: tuple[ToolDef, ...] = (
    ToolDef(
        name="echo",
        description="echo",
        input_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
    ),
    ToolDef(
        name="ls",
        description="list",
        input_schema={"type": "object", "properties": {}},
    ),
)


def typed_messages_four_turn() -> list[Message]:
    """Four-turn conversation: user hi → assistant text+tool → user tool result → assistant."""
    return [
        Message.text("user", "hi"),
        Message(
            role="assistant",
            content=[
                Text(text="ok"),
                ToolRequest(id="c1", name="echo", args={"x": 1}),
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResponse(
                    id="c1",
                    tool_name="echo",
                    result=[Text(text="done")],
                ),
            ],
        ),
        Message.text("assistant", "all set"),
    ]


def typed_messages_turn_2b_tool_only() -> list[Message]:
    """Single assistant turn: tool request only (no Text block)."""
    return [
        Message(
            role="assistant",
            content=[
                ToolRequest(id="c2", name="ls", args={}),
            ],
        ),
    ]
