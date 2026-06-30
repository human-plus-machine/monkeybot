"""Tests for cache_enabled keyword-only constructor flag on all providers."""

from __future__ import annotations

import pytest

from monkeybot.providers.bedrock import BedrockClaudeProvider
from monkeybot.providers.claude import ClaudeProvider
from monkeybot.providers.gemini import GeminiProvider
from monkeybot.providers.huggingface import HuggingFaceProvider
from monkeybot.providers.ollama import OllamaProvider
from monkeybot.providers.openai import OpenAIProvider
from monkeybot.providers.vertex_claude import VertexClaudeProvider


def test_claude_default_cache_enabled_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    provider = ClaudeProvider()
    assert provider._cache_enabled is True


def test_claude_cache_enabled_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    provider = ClaudeProvider(cache_enabled=False)
    assert provider._cache_enabled is False


def test_openai_cache_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    provider = OpenAIProvider(cache_enabled=False)
    assert provider._cache_enabled is False
    default = OpenAIProvider()
    assert default._cache_enabled is True


def test_huggingface_cache_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    provider = HuggingFaceProvider(cache_enabled=False)
    assert provider._cache_enabled is False


def test_ollama_cache_enabled_flag() -> None:
    provider = OllamaProvider(cache_enabled=False)
    assert provider._cache_enabled is False
    default = OllamaProvider()
    assert default._cache_enabled is True


def test_vertex_claude_cache_enabled_flag() -> None:
    provider = VertexClaudeProvider(project_id="p", cache_enabled=False)
    assert provider._cache_enabled is False
    default = VertexClaudeProvider(project_id="p")
    assert default._cache_enabled is True


def test_bedrock_cache_enabled_flag() -> None:
    provider = BedrockClaudeProvider(cache_enabled=False)
    assert provider._cache_enabled is False
    default = BedrockClaudeProvider()
    assert default._cache_enabled is True


def test_gemini_cache_enabled_flag() -> None:
    provider = GeminiProvider(cache_enabled=False)
    assert provider._cache_enabled is False
    default = GeminiProvider()
    assert default._cache_enabled is True


def test_cache_enabled_is_keyword_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    sig = inspect.signature(ClaudeProvider.__init__)
    assert "cache_enabled" in sig.parameters
    assert sig.parameters["cache_enabled"].kind == inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        ClaudeProvider(True)  # type: ignore[misc]
