"""OpenAI provider cache_enabled storage and request kwargs parity."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from types import ModuleType
from typing import Any

import pytest

from monkeybot.core.llm.provider import Message, ProviderEvent
from monkeybot.providers import openai as openai_module
from monkeybot.providers.openai import OpenAIProvider


def _ensure_fake_openai_module(monkeypatch: pytest.MonkeyPatch, fake_client_cls: type) -> None:
    """Inject a minimal ``openai`` module so ``stream()`` lazy import succeeds."""
    fake_openai = ModuleType("openai")
    fake_openai.AsyncOpenAI = fake_client_cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_openai)


def test_openai_stores_cache_enabled_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    provider = OpenAIProvider()
    assert provider._cache_enabled is True


def test_openai_stores_cache_enabled_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    provider = OpenAIProvider(cache_enabled=False)
    assert provider._cache_enabled is False


@pytest.mark.asyncio
async def test_openai_request_kwargs_identical_regardless_of_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    captured: list[dict[str, Any]] = []

    class _FakeAsyncOpenAI:
        def __init__(self, **_kwargs: Any) -> None:
            pass

    async def _record_stream(
        _client: Any, kwargs: dict[str, Any]
    ) -> AsyncIterator[ProviderEvent]:
        captured.append(dict(kwargs))
        if False:  # pragma: no cover — make this an async generator
            yield

    _ensure_fake_openai_module(monkeypatch, _FakeAsyncOpenAI)
    monkeypatch.setattr(openai_module, "iter_openai_compat_stream", _record_stream)

    messages = [Message.text("user", "hello")]
    model = "gpt-4o-mini"

    provider_true = OpenAIProvider(cache_enabled=True)
    async for _ in provider_true.stream(messages, [], model=model):
        pass

    provider_false = OpenAIProvider(cache_enabled=False)
    async for _ in provider_false.stream(messages, [], model=model):
        pass

    assert len(captured) == 2
    assert captured[0] == captured[1]
