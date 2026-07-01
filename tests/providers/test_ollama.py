"""Ollama provider URL resolution and credential-free construction."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from monkeybot.providers._openai_compat import stream_chat_completions_with_tool_fallback
from monkeybot.providers.ollama import (
    _DUMMY_API_KEY,
    OllamaProvider,
    reasoning_effort_for_thinking_budget,
)


def test_reasoning_effort_for_thinking_budget() -> None:
    assert reasoning_effort_for_thinking_budget(-1) is None
    assert reasoning_effort_for_thinking_budget(1024) is None
    assert reasoning_effort_for_thinking_budget(0) == "none"


def test_resolve_base_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    p = OllamaProvider()
    assert p._resolve_base_url("any-model") == "http://localhost:11434/v1"


def test_resolve_base_url_custom_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://my-server:11434")
    p = OllamaProvider()
    assert p._resolve_base_url("m") == "http://my-server:11434/v1"


def test_resolve_base_url_with_v1_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://my-server:11434/v1")
    p = OllamaProvider()
    assert p._resolve_base_url("m") == "http://my-server:11434/v1"


def test_ollama_provider_requires_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    provider = OllamaProvider()
    assert provider.name == "ollama"
    assert provider.supports_streaming is True
    assert provider._api_key == _DUMMY_API_KEY


def test_ollama_uses_custom_api_key_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_API_KEY", "proxy-secret")
    provider = OllamaProvider()
    assert provider._api_key == "proxy-secret"


def test_ollama_stores_cache_enabled_default_true() -> None:
    provider = OllamaProvider()
    assert provider._cache_enabled is True


def test_ollama_stores_cache_enabled_false() -> None:
    provider = OllamaProvider(cache_enabled=False)
    assert provider._cache_enabled is False


def test_ollama_stores_thinking_budget() -> None:
    provider = OllamaProvider(thinking_budget=0)
    assert provider._thinking_budget == 0


def test_ollama_thinking_budget_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_THINKING_BUDGET", "0")
    provider = OllamaProvider()
    assert provider._thinking_budget == 0


@pytest.mark.asyncio
async def test_ollama_stream_passes_reasoning_effort_none(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    async def _fake_stream(**kwargs: Any):
        captured.append(kwargs)
        if False:  # pragma: no cover — async generator stub
            yield

    monkeypatch.setattr(
        "monkeybot.providers.ollama.stream_chat_completions_with_tool_fallback",
        _fake_stream,
    )
    provider = OllamaProvider(thinking_budget=0)
    _ = [ev async for ev in provider.stream([], [], model="gemma4:12b")]
    assert captured[0]["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_ollama_stream_omits_reasoning_effort_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    async def _fake_stream(**kwargs: Any):
        captured.append(kwargs)
        if False:  # pragma: no cover
            yield

    monkeypatch.setattr(
        "monkeybot.providers.ollama.stream_chat_completions_with_tool_fallback",
        _fake_stream,
    )
    provider = OllamaProvider(thinking_budget=-1)
    _ = [ev async for ev in provider.stream([], [], model="gemma4:12b")]
    assert "reasoning_effort" not in captured[0]


@pytest.mark.asyncio
async def test_stream_chat_completions_forwards_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    async def _create(**kwargs: Any) -> Any:
        captured.append(kwargs)

        async def _stream() -> Any:
            if False:  # pragma: no cover
                yield

        return _stream()

    def _fake_openai(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create)),
        )

    monkeypatch.setattr("openai.AsyncOpenAI", _fake_openai)
    _ = [
        ev
        async for ev in stream_chat_completions_with_tool_fallback(
            base_url="http://localhost:11434/v1",
            api_key="ollama",
            provider="ollama",
            messages=[],
            tools=[],
            model="gemma4",
            temperature=0.7,
            max_tokens=100,
            reasoning_effort="none",
        )
    ]
    assert captured[0]["reasoning_effort"] == "none"
