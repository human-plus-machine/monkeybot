"""Provider-neutral multimodal request budget tests."""

from __future__ import annotations

import pytest

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import Image, Text, ToolResponse
from monkeybot.providers.request_budget import (
    RequestByteBudgetError,
    trim_message_media_for_byte_budget,
)


def test_request_budget_stubs_oldest_media_and_keeps_latest() -> None:
    messages = [
        Message(
            role="user",
            content=[Text(text="old"), Image(mime_type="image/png", data="A" * 8_000)],
        ),
        Message(
            role="user",
            content=[Text(text="latest"), Image(mime_type="image/png", data="B" * 200)],
        ),
    ]
    trimmed = trim_message_media_for_byte_budget(
        messages,
        [],
        max_bytes=5_000,
        provider="gemini",
    )
    assert isinstance(trimmed[0].content[1], Text)
    assert isinstance(trimmed[1].content[1], Image)
    assert isinstance(messages[0].content[1], Image)


def test_request_budget_stubs_media_nested_in_tool_response() -> None:
    messages = [
        Message(
            role="user",
            content=[
                ToolResponse(
                    id="call-1",
                    tool_name="load_file",
                    result=[Image(mime_type="image/png", data="A" * 8_000)],
                )
            ],
        )
    ]
    trimmed = trim_message_media_for_byte_budget(
        messages,
        [],
        max_bytes=5_000,
        provider="bedrock",
    )
    response = trimmed[0].content[0]
    assert isinstance(response, ToolResponse)
    assert isinstance(response.result[0], Text)


def test_request_budget_fails_closed_when_text_alone_exceeds_cap() -> None:
    messages = [Message(role="user", content=[Text(text="A" * 8_000)])]
    with pytest.raises(RequestByteBudgetError, match="after removing media"):
        trim_message_media_for_byte_budget(
            messages,
            [],
            max_bytes=5_000,
            provider="claude",
        )
