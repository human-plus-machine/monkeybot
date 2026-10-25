"""Provider-neutral multimodal request budget tests."""

from __future__ import annotations

import json

import pytest

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import File, Image, Text, ToolResponse
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers.request_budget import (
    RequestByteBudgetError,
    _request_bytes,
    configured_request_byte_budget,
    trim_message_media_for_byte_budget,
)


def test_request_budget_stubs_oldest_media_and_keeps_latest() -> None:
    messages = [
        Message(
            role="user",
            content=[
                Text(text="old"),
                Image(
                    mime_type="image/png",
                    data="A" * 8_000,
                    metadata={"attachment_id": "att_old"},
                ),
            ],
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
    assert 'load_file(attachment_id="att_old")' in trimmed[0].content[1].text
    assert isinstance(trimmed[1].content[1], Image)
    assert isinstance(messages[0].content[1], Image)


def test_request_budget_can_preserve_files_for_text_extracting_providers() -> None:
    messages = [
        Message(
            role="user",
            content=[
                File(mime_type="application/pdf", data="A" * 8_000),
                Image(mime_type="image/png", data="B" * 8_000),
            ],
        )
    ]
    trimmed = trim_message_media_for_byte_budget(
        messages,
        [],
        max_bytes=6_000,
        provider="openrouter",
        trim_files=False,
    )
    assert isinstance(trimmed[0].content[0], File)
    assert isinstance(trimmed[0].content[1], Text)


def test_request_budget_stub_is_neutral_and_escapes_reload_argument() -> None:
    messages = [
        Message(
            role="user",
            content=[
                Image(
                    mime_type="image/png",
                    data="A" * 8_000,
                    metadata={"path": 'folder/"quoted".png'},
                )
            ],
        )
    ]
    trimmed = trim_message_media_for_byte_budget(
        messages,
        [],
        max_bytes=5_000,
        provider="gemini",
    )
    stub = trimmed[0].content[0]
    assert isinstance(stub, Text)
    assert "previously shown" not in stub.text
    assert 'load_file(path="folder/\\"quoted\\".png")' in stub.text


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


def test_request_byte_estimate_matches_compact_json_without_copying_base64() -> None:
    messages = [
        Message(
            role="user",
            content=[
                Text(text="look"),
                Image(mime_type="image/png", data="A" * 8_000),
                ToolResponse(
                    id="call-1",
                    tool_name="load_file",
                    result=[Image(mime_type="image/png", data="B" * 4_000)],
                ),
            ],
        )
    ]
    tools = [ToolDef("load_file", "Load a file", {"type": "object"})]
    payload = {
        "messages": [
            {
                "role": message.role,
                "content": [block.to_dict() for block in message.content],
            }
            for message in messages
        ],
        "tools": [tool.to_model_schema() for tool in tools],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    ).encode("utf-8")
    assert _request_bytes(messages, tools) == 4096 + len(encoded)


def test_request_budget_fails_closed_when_text_alone_exceeds_cap() -> None:
    messages = [Message(role="user", content=[Text(text="A" * 8_000)])]
    with pytest.raises(RequestByteBudgetError, match="after removing media"):
        trim_message_media_for_byte_budget(
            messages,
            [],
            max_bytes=5_000,
            provider="claude",
        )


def test_request_budget_allows_token_count_when_text_alone_exceeds_cap() -> None:
    messages = [Message(role="user", content=[Text(text="A" * 8_000)])]
    trimmed = trim_message_media_for_byte_budget(
        messages,
        [],
        max_bytes=5_000,
        provider="claude",
        raise_if_oversized=False,
    )
    assert trimmed == messages


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("openai", 32 * 1024 * 1024),
        ("claude", 32 * 1024 * 1024),
        ("vertex-claude", 32 * 1024 * 1024),
        ("bedrock", 25 * 1024 * 1024),
        ("gemini", 20 * 1024 * 1024),
        ("openrouter", 8 * 1024 * 1024),
    ],
)
def test_provider_request_budget_defaults(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    expected: int,
) -> None:
    monkeypatch.delenv("MODEL_MAX_REQUEST_BYTES", raising=False)
    assert configured_request_byte_budget(provider=provider) == expected


def test_invalid_request_budget_uses_provider_default(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("MODEL_MAX_REQUEST_BYTES", "not-a-number")
    assert configured_request_byte_budget(provider="gemini") == 20 * 1024 * 1024
    assert "invalid MODEL_MAX_REQUEST_BYTES" in caplog.text
