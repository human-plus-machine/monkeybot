"""Ollama provider URL resolution and credential-free construction."""

from __future__ import annotations

import pytest

from monkeybot.providers.ollama import _DUMMY_API_KEY, OllamaProvider


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
