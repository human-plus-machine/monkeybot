"""Byte-budget trimming and body-read 400 retry for OpenAI-compat providers."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from monkeybot.core.llm.provider import Message, TextDelta, UsageEvent
from monkeybot.core.types.content_blocks import Image, Text
from monkeybot.providers._openai_compat import (
    _OMITTED_IMAGE_TEXT,
    is_request_body_read_error,
    openai_chat_request_body_bytes,
    stream_chat_completions_with_tool_fallback,
    trim_openai_messages_for_byte_budget,
)


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
        .dumps({"model": "m", "messages": kwargs["messages"]}, ensure_ascii=False)
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
    out, stubbed = trim_openai_messages_for_byte_budget(kwargs, 4_000)
    after = openai_chat_request_body_bytes(out)
    assert stubbed >= 1
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
    out, stubbed = trim_openai_messages_for_byte_budget(kwargs, 1, drop_all_media=True)
    assert stubbed == 2
    for msg in out["messages"]:
        for part in msg["content"]:
            if isinstance(part, dict):
                assert part.get("type") != "image_url"


class _FakeAPIError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _FakeAPIStatusError(_FakeAPIError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = {"error": {"message": message, "type": "invalid_request_error"}}


def test_is_request_body_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_openai = ModuleType("openai")
    fake_openai.APIStatusError = _FakeAPIStatusError
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    hit = _FakeAPIStatusError(400, "failed to read request body")
    miss = _FakeAPIStatusError(400, "invalid json schema")
    other = _FakeAPIStatusError(500, "failed to read request body")
    assert is_request_body_read_error(hit)
    assert not is_request_body_read_error(miss)
    assert not is_request_body_read_error(other)


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
                )
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
    assert any(isinstance(ev, (UsageEvent, TextDelta)) for ev in events)
