"""The yielding Gemini conversion matches the sync one."""

from __future__ import annotations

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import Text, Thinking, ToolRequest, ToolResponse
from monkeybot.providers.gemini import (
    THOUGHT_SIGNATURE_KEY,
    _messages_to_contents,
    _messages_to_contents_sliced,
)


def _history() -> list[Message]:
    """An older tool turn, then an active turn with thinking and a signed tool call."""
    return [
        Message(role="user", content=[Text(text="first")]),
        Message(
            role="assistant",
            content=[
                ToolRequest(
                    id="old",
                    name="noop",
                    args={},
                    metadata={THOUGHT_SIGNATURE_KEY: "stale-sig"},
                )
            ],
        ),
        Message(
            role="user",
            content=[ToolResponse(id="old", tool_name="noop", result=[Text(text="ok")])],
        ),
        Message(role="user", content=[Text(text="now do this")]),
        Message(
            role="assistant",
            content=[
                Thinking(thinking="reasoning step", signature="sig-think"),
                ToolRequest(
                    id="cur",
                    name="search",
                    args={"q": "x"},
                    metadata={THOUGHT_SIGNATURE_KEY: "sig-tool"},
                ),
            ],
        ),
    ]


async def test_sliced_contents_match_the_sync_conversion() -> None:
    rest = _history()
    assert await _messages_to_contents_sliced(rest) == _messages_to_contents(rest)
