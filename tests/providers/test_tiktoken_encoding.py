"""The shared tiktoken encoding is loaded once, at import."""

from __future__ import annotations

import uuid

import pytest

from monkeybot.core.llm.provider import Message
from monkeybot.providers._openai_compat import (
    _TIKTOKEN_FALLBACK_ENCODING,
    _tiktoken_encodings,
    count_input_tokens_tiktoken,
    tiktoken_encoding_for_model,
)


def test_unknown_model_reuses_the_preloaded_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    import tiktoken

    model = f"not-a-tiktoken-model-{uuid.uuid4()}"
    calls: list[str] = []
    real_get = tiktoken.get_encoding

    def _spy(name: str):
        calls.append(name)
        return real_get(name)

    monkeypatch.setattr(tiktoken, "get_encoding", _spy)

    enc = tiktoken_encoding_for_model(model)
    again = tiktoken_encoding_for_model(model)

    assert calls == []
    assert enc is again
    assert enc is real_get(_TIKTOKEN_FALLBACK_ENCODING)
    assert _tiktoken_encodings[_TIKTOKEN_FALLBACK_ENCODING] is enc


def test_known_model_keeps_its_own_encoding() -> None:
    import tiktoken

    enc = tiktoken_encoding_for_model("gpt-4o")

    assert enc is tiktoken.get_encoding("o200k_base")
    assert enc is not tiktoken.get_encoding(_TIKTOKEN_FALLBACK_ENCODING)


def test_missing_fallback_still_loads_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import tiktoken

    model = f"not-a-tiktoken-model-{uuid.uuid4()}"
    saved = _tiktoken_encodings.pop(_TIKTOKEN_FALLBACK_ENCODING, None)
    calls: list[str] = []
    real_get = tiktoken.get_encoding

    def _spy(name: str):
        calls.append(name)
        return real_get(name)

    monkeypatch.setattr(tiktoken, "get_encoding", _spy)
    try:
        first = tiktoken_encoding_for_model(model)
        second = tiktoken_encoding_for_model(model)
    finally:
        if _TIKTOKEN_FALLBACK_ENCODING not in _tiktoken_encodings and saved is not None:
            _tiktoken_encodings[_TIKTOKEN_FALLBACK_ENCODING] = saved

    assert calls == [_TIKTOKEN_FALLBACK_ENCODING]
    assert first is second
    assert _tiktoken_encodings[_TIKTOKEN_FALLBACK_ENCODING] is first


@pytest.mark.asyncio
async def test_count_for_an_unknown_model_is_stable() -> None:
    messages = [Message.text("user", "Reply with exactly the word pong.")]
    kwargs = {
        "model": "glm-5.3-flash",
        "provider": "ollama-cloud",
        "max_request_bytes": 8 * 1024 * 1024,
    }

    first = await count_input_tokens_tiktoken(messages, [], **kwargs)
    second = await count_input_tokens_tiktoken(messages, [], **kwargs)

    assert first > 0
    assert first == second
