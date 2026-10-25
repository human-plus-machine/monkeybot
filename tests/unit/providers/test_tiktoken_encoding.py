"""The shared tiktoken encoding is loaded once, at import."""

from __future__ import annotations

import logging
import uuid

import pytest

from monkeybot.core.llm.provider import Message
from monkeybot.providers._openai_compat import (
    _TIKTOKEN_FALLBACK_ENCODING,
    _load_fallback_tiktoken_encoding,
    _tiktoken_encodings,
    count_input_tokens_tiktoken,
    tiktoken_encoding_for_model,
)

# tiktoken/load.py fetches this URL with requests.get when the on-disk cache misses.
# urllib3 raises requests.ConnectionError; the text is that pool error for this host.
_COLD_CACHE_CONNECTION_ERROR = (
    "HTTPSConnectionPool(host='openaipublic.blob.core.windows.net', port=443): "
    "Max retries exceeded with url: /encodings/cl100k_base.tiktoken "
    "(Caused by NewConnectionError("
    "'<urllib3.connection.HTTPSConnection object>: Failed to establish a new connection: "
    "[Errno 8] nodename nor servname provided, or not known'))"
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


def test_preload_failure_does_not_break_import_or_poison_the_cache(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cold cache that cannot reach the encoding file must not fail the import.

    The count path still raises that error, and a later successful read is used.
    """
    import requests
    import tiktoken

    model = f"not-a-tiktoken-model-{uuid.uuid4()}"
    other = f"not-a-tiktoken-model-{uuid.uuid4()}"
    saved = _tiktoken_encodings.pop(_TIKTOKEN_FALLBACK_ENCODING, None)
    real_get = tiktoken.get_encoding
    load_attempted = False

    def _fail_the_preload(name: str):
        nonlocal load_attempted
        if not load_attempted:
            load_attempted = True
            raise requests.exceptions.ConnectionError(_COLD_CACHE_CONNECTION_ERROR)
        return real_get(name)

    monkeypatch.setattr(tiktoken, "get_encoding", _fail_the_preload)
    try:
        with caplog.at_level(logging.WARNING, logger="monkeybot.providers._openai_compat"):
            _load_fallback_tiktoken_encoding()
        assert _TIKTOKEN_FALLBACK_ENCODING not in _tiktoken_encodings
        assert any(_TIKTOKEN_FALLBACK_ENCODING in record.message for record in caplog.records)

        enc = tiktoken_encoding_for_model(model)
        assert enc is real_get(_TIKTOKEN_FALLBACK_ENCODING)
        assert _tiktoken_encodings[_TIKTOKEN_FALLBACK_ENCODING] is enc

        def _stay_offline(name: str):
            raise requests.exceptions.ConnectionError(_COLD_CACHE_CONNECTION_ERROR)

        monkeypatch.setattr(tiktoken, "get_encoding", _stay_offline)
        _tiktoken_encodings.pop(_TIKTOKEN_FALLBACK_ENCODING, None)
        _tiktoken_encodings.pop(model, None)
        with pytest.raises(requests.exceptions.ConnectionError, match="cl100k_base.tiktoken"):
            tiktoken_encoding_for_model(other)
        assert other not in _tiktoken_encodings
        assert _TIKTOKEN_FALLBACK_ENCODING not in _tiktoken_encodings

        def _out_of_memory(name: str):
            raise MemoryError

        monkeypatch.setattr(tiktoken, "get_encoding", _out_of_memory)
        with pytest.raises(MemoryError):
            _load_fallback_tiktoken_encoding()
        assert _TIKTOKEN_FALLBACK_ENCODING not in _tiktoken_encodings
    finally:
        _tiktoken_encodings.pop(model, None)
        _tiktoken_encodings.pop(other, None)
        if saved is not None:
            _tiktoken_encodings[_TIKTOKEN_FALLBACK_ENCODING] = saved
        else:
            _tiktoken_encodings.pop(_TIKTOKEN_FALLBACK_ENCODING, None)


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
