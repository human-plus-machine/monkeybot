"""Gemini usage telemetry and cache_enabled constructor contract."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from monkeybot.core.llm.provider import UsageEvent
from monkeybot.core.types.interfaces import LLMError
from monkeybot.providers.gemini import GeminiProvider, _usage_from_response

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GEMINI_SRC = _REPO_ROOT / "src/monkeybot/providers/gemini.py"


def test_usage_maps_cached_count_to_split_fields() -> None:
    um = types.SimpleNamespace(
        prompt_token_count=1200,
        candidates_token_count=80,
        cached_content_token_count=500,
    )
    ev = _usage_from_response(um)
    assert ev == UsageEvent(
        input_tokens=1200,
        output_tokens=80,
        cached_tokens=500,
        cache_read_tokens=500,
        cache_creation_tokens=0,
    )


def test_usage_no_cached_count_zeroes_cache_fields() -> None:
    um = types.SimpleNamespace(
        prompt_token_count=300,
        candidates_token_count=40,
    )
    ev = _usage_from_response(um)
    assert ev == UsageEvent(
        input_tokens=300,
        output_tokens=40,
        cached_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
    )


def test_usage_cached_count_none_coerces_to_zero() -> None:
    um = types.SimpleNamespace(
        prompt_token_count=300,
        candidates_token_count=40,
        cached_content_token_count=None,
    )
    ev = _usage_from_response(um)
    assert ev is not None
    assert ev.cached_tokens == 0
    assert ev.cache_read_tokens == 0
    assert ev.cache_creation_tokens == 0


def test_usage_invariant_total_equals_read_plus_creation() -> None:
    um = types.SimpleNamespace(
        prompt_token_count=1200,
        candidates_token_count=80,
        cached_content_token_count=500,
    )
    ev = _usage_from_response(um)
    assert ev is not None
    assert ev.cached_tokens == ev.cache_read_tokens + ev.cache_creation_tokens


def test_usage_none_metadata_returns_none() -> None:
    assert _usage_from_response(None) is None


def test_init_defaults_cache_enabled_true() -> None:
    provider = GeminiProvider()
    assert provider._cache_enabled is True


def test_init_stores_cache_enabled_false() -> None:
    provider = GeminiProvider(cache_enabled=False)
    assert provider._cache_enabled is False


def test_init_cache_enabled_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        GeminiProvider(True)  # type: ignore[misc]


def test_request_config_identical_regardless_of_cache_flag() -> None:
    enabled = GeminiProvider(
        temperature=0.5,
        max_output_tokens=4096,
        thinking_budget=1024,
        cache_enabled=True,
    )
    disabled = GeminiProvider(
        temperature=0.5,
        max_output_tokens=4096,
        thinking_budget=1024,
        cache_enabled=False,
    )
    for attr in (
        "_temperature",
        "_max_tokens",
        "_thinking_budget",
        "_supports_streaming",
    ):
        assert getattr(enabled, attr) == getattr(disabled, attr)

    src = _GEMINI_SRC.read_text(encoding="utf-8")
    init_end = src.index("    @property")
    init_block = src[:init_end]
    remainder = src[init_end:]
    assert init_block.count("self._cache_enabled") == 1
    assert "self._cache_enabled" not in remainder


@pytest.mark.asyncio
async def test_stream_error_logs_before_wrapping(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FakeModels:
        async def generate_content_stream(self, **_kwargs: Any) -> Any:
            raise RuntimeError("boom")

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.aio = types.SimpleNamespace(models=FakeModels())

    fake_google = ModuleType("google")
    fake_genai = ModuleType("google.genai")
    fake_types = ModuleType("google.genai.types")
    fake_genai.Client = FakeClient  # type: ignore[attr-defined]
    fake_types.GenerateContentConfig = lambda **kw: kw  # type: ignore[attr-defined]
    fake_types.ThinkingConfig = lambda **kw: kw  # type: ignore[attr-defined]
    fake_types.Tool = lambda **kw: kw  # type: ignore[attr-defined]
    fake_types.FunctionDeclaration = lambda **kw: kw  # type: ignore[attr-defined]
    fake_google.genai = fake_genai  # type: ignore[attr-defined]
    fake_genai.types = fake_types  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    monkeypatch.setenv("VERTEX_AI_PROJECT_ID", "p")

    provider = GeminiProvider()
    with (
        caplog.at_level("WARNING", logger="monkeybot.providers.gemini"),
        pytest.raises(LLMError, match="boom"),
    ):
        [ev async for ev in provider.stream([], [], model="gemini-2.5-flash")]

    assert "Gemini stream error" in caplog.text
    assert "provider=gemini" in caplog.text
    assert "model=gemini-2.5-flash" in caplog.text
