"""Gateway provider resolution via get_provider_config."""

from __future__ import annotations

import pytest

from monkeybot.core.config.settings import normalize_model_provider
from monkeybot.core.llm.provider import Done, TextDelta
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.gateway.sse import app as gateway_app
from monkeybot.providers.gemini import GeminiProvider
from monkeybot.providers.huggingface import HuggingFaceProvider


def test_normalize_model_provider_aliases() -> None:
    assert normalize_model_provider("gemini") == "google_vertexai"
    assert normalize_model_provider("vertex") == "google_vertexai"
    assert normalize_model_provider("vertex-claude") == "vertex_anthropic"
    assert normalize_model_provider("huggingface") == "huggingface"


def test_resolve_provider_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    provider = gateway_app._resolve_provider()
    assert isinstance(provider, ScriptedFakeProvider)


def test_resolve_provider_huggingface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "huggingface")
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    provider = gateway_app._resolve_provider()
    assert isinstance(provider, HuggingFaceProvider)


def test_resolve_provider_gemini_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    provider = gateway_app._resolve_provider()
    assert isinstance(provider, GeminiProvider)


def test_resolve_curator_reuses_main_for_vertex_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "vertex-claude")
    main = ScriptedFakeProvider([TextDelta(text="x"), Done()])
    curator = gateway_app._resolve_curator_provider(main)
    assert curator is main
