"""Ollama provider URL resolution and credential-free construction."""

from __future__ import annotations

from typing import Any

import pytest

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
