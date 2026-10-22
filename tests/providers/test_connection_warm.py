"""Startup connection warm-up uses the cached client and does not complete a chat."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from monkeybot.core.llm.provider import Message
from monkeybot.providers._openai_compat import (
    openai_connection_limits,
    warm_provider_connection,
)
from monkeybot.providers._sdk_client import aclose_sdk_clients
from monkeybot.providers.ollama import OllamaProvider


@pytest.fixture(autouse=True)
async def _clear_sdk_clients() -> Any:
    await aclose_sdk_clients()
    yield
    await aclose_sdk_clients()


def test_connection_limits_keep_sdk_caps_and_extend_idle() -> None:
    limits = openai_connection_limits()
    assert limits.max_connections == 1000
    assert limits.max_keepalive_connections == 100
    assert limits.keepalive_expiry == 120.0


def _chunk() -> SimpleNamespace:
    return SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                delta=SimpleNamespace(content="hi", tool_calls=None),
            )
        ],
    )


def _install_openai(
    monkeypatch: pytest.MonkeyPatch,
    *,
    list_models: Any,
) -> dict[str, Any]:
    seen: dict[str, Any] = {
        "clients": [],
        "http_kwargs": [],
        "client_kwargs": [],
        "completions": [],
    }

    class _Completions:
        async def create(self, **kwargs: Any) -> Any:
            seen["completions"].append(kwargs)

            async def _stream() -> Any:
                yield _chunk()

            return _stream()

    def _http(**kwargs: Any) -> SimpleNamespace:
        seen["http_kwargs"].append(kwargs)
        return SimpleNamespace(**kwargs)

    def _factory(**kwargs: Any) -> SimpleNamespace:
        client = SimpleNamespace(
            models=SimpleNamespace(list=list_models),
            chat=SimpleNamespace(completions=_Completions()),
        )
        seen["clients"].append(client)
        seen["client_kwargs"].append(kwargs)
        return client

    fake = ModuleType("openai")
    fake.AsyncOpenAI = _factory  # type: ignore[attr-defined]
    fake.DefaultAsyncHttpxClient = _http  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-test")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    return seen


def _provider() -> OllamaProvider:
    return OllamaProvider(mode="cloud", temperature=0.0, max_tokens=16, thinking_budget=0)


@pytest.mark.asyncio
async def test_warm_and_stream_share_one_client(monkeypatch: pytest.MonkeyPatch) -> None:
    listed: list[float] = []

    async def _list(**_kwargs: Any) -> list[object]:
        listed.append(1)
        return []

    seen = _install_openai(monkeypatch, list_models=_list)
    provider = _provider()

    await provider.warm_connection()

    assert listed == [1]
    assert seen["completions"] == []
    assert len(seen["clients"]) == 1
    limits = seen["http_kwargs"][0]["limits"]
    assert limits.max_connections == 1000
    assert limits.max_keepalive_connections == 100
    assert limits.keepalive_expiry == 120.0
    assert seen["http_kwargs"][0]["base_url"] == "https://ollama.com/v1"
    assert seen["client_kwargs"][0]["base_url"] == "https://ollama.com/v1"
    assert seen["client_kwargs"][0]["api_key"] == "ollama-test"

    async for _event in provider.stream([Message.text("user", "hi")], [], model="glm-test"):
        pass

    assert len(seen["clients"]) == 1
    assert len(seen["completions"]) == 1
    assert listed == [1]


@pytest.mark.asyncio
async def test_warm_swallows_probe_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _list(**_kwargs: Any) -> list[object]:
        raise RuntimeError("models down")

    seen = _install_openai(monkeypatch, list_models=_list)
    await _provider().warm_connection()
    assert seen["completions"] == []


@pytest.mark.asyncio
async def test_warm_provider_connection_skips_providers_without_the_method() -> None:
    await warm_provider_connection(None)
    await warm_provider_connection(object())
    await warm_provider_connection(MagicMock())


@pytest.mark.asyncio
async def test_warm_provider_connection_swallows_method_errors() -> None:
    class _Boom:
        async def warm_connection(self) -> None:
            raise RuntimeError("boom")

    await warm_provider_connection(_Boom())
