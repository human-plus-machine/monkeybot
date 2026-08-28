"""Gateway provider resolution via get_provider_config."""

from __future__ import annotations

import pytest

from monkeybot.core.config.settings import normalize_model_provider
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.gateway.sse import app as gateway_app
from monkeybot.providers.gemini import GeminiProvider
from monkeybot.providers.huggingface import HuggingFaceProvider
from monkeybot.providers.ollama import (
    _DUMMY_API_KEY,
    OllamaProvider,
)
from monkeybot.providers.openrouter import OpenRouterProvider


def test_normalize_model_provider_aliases() -> None:
    assert normalize_model_provider("gemini") == "google_vertexai"
    assert normalize_model_provider("vertex") == "google_vertexai"
    assert normalize_model_provider("vertex-claude") == "vertex_anthropic"
    assert normalize_model_provider("huggingface") == "huggingface"
    assert normalize_model_provider("ollama_cloud") == "ollama-cloud"
    assert normalize_model_provider("ollama-local") == "ollama-local"


def test_resolve_provider_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    provider = gateway_app._resolve_provider()
    assert isinstance(provider, ScriptedFakeProvider)


def test_resolve_provider_huggingface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "huggingface")
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    provider = gateway_app._resolve_provider()
    assert isinstance(provider, HuggingFaceProvider)


def test_resolve_provider_ollama_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "ollama-cloud")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-cloud-key")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    provider = gateway_app._resolve_provider()
    assert isinstance(provider, OllamaProvider)
    assert provider.name == "ollama-cloud"
    assert provider._resolve_base_url("glm-5.3-flash") == "https://ollama.com/v1"


def test_resolve_provider_ollama_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "ollama-local")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-cloud-key")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    provider = gateway_app._resolve_provider()
    assert isinstance(provider, OllamaProvider)
    assert provider.name == "ollama-local"
    assert provider._api_key == _DUMMY_API_KEY
    assert provider._resolve_base_url("llama3.1") == "http://localhost:11434/v1"


def test_resolve_provider_gemini_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    provider = gateway_app._resolve_provider()
    assert isinstance(provider, GeminiProvider)


def test_resolve_provider_google_genai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "google_genai")
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")
    provider = gateway_app._resolve_provider()
    assert isinstance(provider, GeminiProvider)


def test_resolve_provider_google_genai_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "google_genai")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        gateway_app._resolve_provider()


def test_resolve_provider_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    provider = gateway_app._resolve_provider()
    assert isinstance(provider, OpenRouterProvider)
    assert provider._base_url == "https://openrouter.ai/api/v1"


def test_openrouter_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterProvider()
