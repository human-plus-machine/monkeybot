"""Ollama provider URL resolution and credential-free construction."""

from __future__ import annotations

import logging
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
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
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


def test_api_key_without_url_targets_ollama_cloud(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-cloud-key")
    with caplog.at_level(logging.WARNING, logger="monkeybot.providers.ollama"):
        provider = OllamaProvider()
    assert provider._api_key == "ollama-cloud-key"
    assert provider._resolve_base_url("gpt-oss:120b") == "https://ollama.com/v1"
    assert "host=https://ollama.com" in caplog.text
    assert "local reverse proxy" in caplog.text


def test_explicit_url_wins_over_cloud_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Key + explicit URL keeps local/proxy routing (pre-cloud behavior)."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_API_KEY", "proxy-secret")
    provider = OllamaProvider()
    assert provider._api_key == "proxy-secret"
    assert provider._resolve_base_url("m") == "http://localhost:11434/v1"


def test_cloud_mode_ignores_local_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-cloud-key")
    with caplog.at_level(logging.WARNING, logger="monkeybot.providers.ollama"):
        provider = OllamaProvider(mode="cloud")
    assert provider.name == "ollama-cloud"
    assert provider._api_key == "ollama-cloud-key"
    assert provider._resolve_base_url("glm-5.3-flash") == "https://ollama.com/v1"
    assert "ignoring non-cloud OLLAMA_BASE_URL" in caplog.text
    assert "ignored=http://127.0.0.1:11434" in caplog.text


def test_cloud_mode_keeps_explicit_cloud_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-cloud-key")
    provider = OllamaProvider(mode="cloud")
    assert provider._resolve_base_url("m") == "https://ollama.com/v1"


def test_cloud_mode_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    with pytest.raises(ValueError, match="OLLAMA_API_KEY is not set"):
        OllamaProvider(mode="cloud")


def test_local_mode_ignores_cloud_url_and_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-cloud-key")
    with caplog.at_level(logging.WARNING, logger="monkeybot.providers.ollama"):
        provider = OllamaProvider(mode="local")
    assert provider.name == "ollama-local"
    assert provider._api_key == _DUMMY_API_KEY
    assert provider._resolve_base_url("llama3.1") == "http://localhost:11434/v1"
    assert "ignoring cloud OLLAMA_BASE_URL" in caplog.text


def test_local_mode_uses_custom_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://my-server:11434")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-cloud-key")
    provider = OllamaProvider(mode="local")
    assert provider._api_key == _DUMMY_API_KEY
    assert provider._resolve_base_url("m") == "http://my-server:11434/v1"


def test_provider_logs_resolved_host(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    with caplog.at_level(logging.INFO, logger="monkeybot.providers.ollama"):
        OllamaProvider(mode="local")
    assert "ollama host resolved" in caplog.text
    assert "mode=local" in caplog.text
    assert "host=http://localhost:11434" in caplog.text


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
    assert captured[0]["provider"] == "ollama"


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
    assert captured[0]["provider"] == "ollama"


@pytest.mark.asyncio
async def test_cloud_stream_passes_provider_name(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    async def _fake_stream(**kwargs: Any):
        captured.append(kwargs)
        if False:  # pragma: no cover
            yield

    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-cloud-key")
    monkeypatch.setattr(
        "monkeybot.providers.ollama.stream_chat_completions_with_tool_fallback",
        _fake_stream,
    )
    provider = OllamaProvider(mode="cloud")
    _ = [ev async for ev in provider.stream([], [], model="glm-5.3-flash")]
    assert captured[0]["provider"] == "ollama-cloud"
