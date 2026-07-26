"""Shared typed message fixtures for provider adapter tests (Story 3)."""

from __future__ import annotations

from unittest.mock import MagicMock

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import Text, ToolRequest, ToolResponse
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


def make_anthropic_stream_mock(events: list[object]) -> MagicMock:
    """Async context manager mock for ``client.messages.stream(...)``."""

    class _AsyncStreamCM:
        async def __aenter__(self) -> object:
            async def _gen() -> object:
                for event in events:
                    yield event

            return _gen()

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    client = MagicMock()
    # New CM per call so a retried request re-reads the events, not an exhausted generator.
    client.messages.stream = MagicMock(side_effect=lambda **_kw: _AsyncStreamCM())
    return client


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
