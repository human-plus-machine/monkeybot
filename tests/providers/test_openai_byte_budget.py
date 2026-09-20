"""Byte-budget trimming and body-read 400 retry for OpenAI-compat providers."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from monkeybot.core.llm.provider import Message, TextDelta, UsageEvent
from monkeybot.core.types.content_blocks import File, Image, Text, ToolResponse
from monkeybot.providers._openai_compat import (
    _OMITTED_IMAGE_TEXT,
    _trim_openai_messages_for_byte_budget,
    enforce_openai_request_byte_budget,
    is_ollama_request_body_read_error,
    openai_chat_request_body_bytes,
    stream_chat_completions_with_tool_fallback,
)
from monkeybot.providers.openai import OpenAIProvider
from monkeybot.providers.request_budget import RequestByteBudgetError


def _data_url(n: int) -> str:
    return "data:image/png;base64," + ("A" * n)


def _image_message(url: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "see this"},
            {"type": "image_url", "image_url": {"url": url}},
        ],
    }


def test_openai_chat_request_body_bytes_counts_json() -> None:
    kwargs = {
        "model": "m",
        "messages": [_image_message(_data_url(100))],
        "stream": True,
    }
    n = openai_chat_request_body_bytes(kwargs)
    assert n > 100
    assert n == len(
        __import__("json")
        .dumps(
            {"model": "m", "messages": kwargs["messages"], "stream": True},
            ensure_ascii=False,
        )
        .encode("utf-8")
    )


def test_trim_drops_oldest_images_first() -> None:
    kwargs = {
        "model": "m",
        "messages": [
            _image_message(_data_url(8_000)),
            _image_message(_data_url(8_000)),
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "latest"},
                    {"type": "image_url", "image_url": {"url": _data_url(200)}},
                ],
            },
        ],
    }
    before = openai_chat_request_body_bytes(kwargs)
    out = enforce_openai_request_byte_budget(
        kwargs,
        4_000,
        provider="openrouter",
        model="m",
    )
    after = openai_chat_request_body_bytes(out)
    assert after < before
    assert after <= 4_000
    last = out["messages"][-1]["content"]
    assert any(isinstance(p, dict) and p.get("type") == "image_url" for p in last)
    first = out["messages"][0]["content"]
    assert any(
        isinstance(p, dict)
        and p.get("type") == "text"
        and _OMITTED_IMAGE_TEXT in str(p.get("text"))
        for p in first
    )
    roles = [m["role"] for m in out["messages"]]
    assert roles == [m["role"] for m in kwargs["messages"]]


def test_trim_drop_all_media_stubs_every_data_url() -> None:
    kwargs = {
        "model": "m",
        "messages": [_image_message(_data_url(4_000)), _image_message(_data_url(4_000))],
    }
    out, stubbed, _after = _trim_openai_messages_for_byte_budget(
        kwargs,
        1,
        drop_all_media=True,
    )
    assert stubbed == 2
    for msg in out["messages"]:
        for part in msg["content"]:
            if isinstance(part, dict):
                assert part.get("type") != "image_url"


def test_trim_stops_destroying_tool_text_once_under_budget() -> None:
    kwargs = {
        "model": "m",
        "messages": [
            {"role": "tool", "content": "A" * 5_000},
            {"role": "tool", "content": "B" * 5_000},
        ],
    }
    before = openai_chat_request_body_bytes(kwargs)
    out = enforce_openai_request_byte_budget(
        kwargs,
        before - 3_000,
        provider="openrouter",
        model="m",
    )
    assert len(out["messages"][0]["content"]) < 5_000
    assert out["messages"][1]["content"] == "B" * 5_000


def test_drop_all_media_without_lower_cap_preserves_tool_text() -> None:
    kwargs = {
        "model": "m",
        "messages": [
            {"role": "tool", "content": "A" * 5_000},
            _image_message(_data_url(4_000)),
        ],
    }
    before = openai_chat_request_body_bytes(kwargs)
    out, stubbed, _after = _trim_openai_messages_for_byte_budget(
        kwargs,
        before,
        drop_all_media=True,
    )
    assert stubbed == 1
    assert out["messages"][0]["content"] == "A" * 5_000


class _FakeAPIError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _FakeAPIStatusError(_FakeAPIError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = {"error": {"message": message, "type": "invalid_request_error"}}


def test_is_ollama_request_body_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_openai = ModuleType("openai")
    fake_openai.APIStatusError = _FakeAPIStatusError
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    hit = _FakeAPIStatusError(400, "failed to read request body")
    miss = _FakeAPIStatusError(400, "invalid json schema")
    other = _FakeAPIStatusError(500, "failed to read request body")
    assert is_ollama_request_body_read_error(hit)
    assert not is_ollama_request_body_read_error(miss)
    assert not is_ollama_request_body_read_error(other)


def _text_chunk(content: str = "hi") -> SimpleNamespace:
    return SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(delta=SimpleNamespace(content=content, reasoning=None, tool_calls=None))
        ],
    )


@pytest.mark.asyncio
async def test_body_read_400_retries_once_without_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []
    fail = _FakeAPIStatusError(400, "failed to read request body")
    calls = {"n": 0}

    async def _create(**kwargs: Any) -> Any:
        calls["n"] += 1
        captured.append(kwargs)
        if calls["n"] == 1:
            raise fail

        async def _stream() -> Any:
            yield _text_chunk("ok")

        return _stream()

    fake_openai = ModuleType("openai")
    fake_openai.AsyncOpenAI = lambda *_a, **_kw: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )
    fake_openai.APIStatusError = _FakeAPIStatusError
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    events = [
        ev
        async for ev in stream_chat_completions_with_tool_fallback(
            base_url="https://ollama.com/v1",
            api_key="key",
            provider="ollama",
            messages=[
                Message(
                    role="user",
                    content=[
                        Text(text="look"),
                        Image(
                            mime_type="image/png", data="A" * 8_000, metadata={"filename": "x.png"}
                        ),
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ToolResponse(
                            id="tool-1",
                            tool_name="read_file",
                            result=[Text(text="T" * 5_000)],
                        )
                    ],
                ),
            ],
            tools=[],
            model="gpt-oss:20b",
            temperature=0.2,
            max_tokens=64,
            max_request_bytes=50_000_000,
        )
    ]
    assert calls["n"] == 2
    first_msgs = captured[0]["messages"]
    second_msgs = captured[1]["messages"]

    def _has_data_url(msgs: list[dict[str, Any]]) -> bool:
        for msg in msgs:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                image_url = part.get("image_url")
                url = image_url.get("url") if isinstance(image_url, dict) else image_url
                if str(url).startswith("data:"):
                    return True
        return False

    assert _has_data_url(first_msgs)
    assert not _has_data_url(second_msgs)
    first_tool = next(msg for msg in first_msgs if msg.get("role") == "tool")
    second_tool = next(msg for msg in second_msgs if msg.get("role") == "tool")
    assert second_tool["content"] == first_tool["content"]
    assert any(isinstance(ev, (UsageEvent, TextDelta)) for ev in events)


@pytest.mark.asyncio
async def test_non_ollama_body_read_400_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    fail = _FakeAPIStatusError(400, "failed to read request body")

    async def _create(**_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise fail

    fake_openai = ModuleType("openai")
    fake_openai.AsyncOpenAI = lambda *_a, **_kw: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )
    fake_openai.APIStatusError = _FakeAPIStatusError
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    with pytest.raises(_FakeAPIStatusError):
        _ = [
            event
            async for event in stream_chat_completions_with_tool_fallback(
                base_url="https://openrouter.ai/api/v1",
                api_key="key",
                provider="openrouter",
                messages=[Message(role="user", content=[Text(text="hello")])],
                tools=[],
                model="m",
                temperature=0.2,
                max_tokens=64,
                max_request_bytes=50_000,
            )
        ]
    assert calls == 1


@pytest.mark.asyncio
async def test_compat_budget_extracts_oversized_pdf_before_enforcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []
    extracted: list[File] = []

    async def _extract_pdf_text(block: File) -> str:
        extracted.append(block)
        return "important extracted PDF text"

    async def _create(**kwargs: Any) -> Any:
        captured.append(kwargs)

        async def _stream() -> Any:
            yield _text_chunk("ok")

        return _stream()

    fake_openai = ModuleType("openai")
    fake_openai.AsyncOpenAI = lambda *_a, **_kw: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    monkeypatch.setattr(
        "monkeybot.providers._openai_compat._extract_pdf_text",
        _extract_pdf_text,
    )

    _ = [
        event
        async for event in stream_chat_completions_with_tool_fallback(
            base_url="https://openrouter.ai/api/v1",
            api_key="key",
            provider="openrouter",
            messages=[
                Message(
                    role="user",
                    content=[
                        File(
                            mime_type="application/pdf",
                            data="A" * 8_000,
                            metadata={"filename": "report.pdf"},
                        )
                    ],
                )
            ],
            tools=[],
            model="m",
            temperature=0.2,
            max_tokens=64,
            max_request_bytes=5_000,
        )
    ]

    assert len(extracted) == 1
    content = captured[0]["messages"][0]["content"]
    assert any("important extracted PDF text" in part["text"] for part in content)


@pytest.mark.asyncio
async def test_direct_openai_checks_budget_after_file_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("MODEL_MAX_REQUEST_BYTES", "10000")

    async def _messages_to_openai(
        _messages: list[Message],
    ) -> tuple[None, list[dict[str, Any]]]:
        assert isinstance(_messages[0].content[0], File)
        return None, [{"role": "user", "content": "X" * 20_000}]

    fake_openai = ModuleType("openai")
    fake_openai.AsyncOpenAI = lambda **_kwargs: SimpleNamespace()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    monkeypatch.setattr("monkeybot.providers.openai.messages_to_openai", _messages_to_openai)

    provider = OpenAIProvider()
    with pytest.raises(RequestByteBudgetError, match="after trimming"):
        _ = [
            event
            async for event in provider.stream(
                [
                    Message(
                        role="user",
                        content=[File(mime_type="application/pdf", data="A" * 20_000)],
                    )
                ],
                [],
                model="gpt-4.1",
            )
        ]


@pytest.mark.asyncio
async def test_direct_openai_token_count_can_trigger_compaction_over_byte_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("MODEL_MAX_REQUEST_BYTES", "1000")
    provider = OpenAIProvider()

    count = await provider.count_input_tokens(
        [Message(role="user", content=[Text(text="history " * 2_000)])],
        [],
        model="gpt-4.1",
    )

    assert count > 1_000
