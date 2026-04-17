"""MP-C-01 / MP-C-02 for :class:`OllamaProvider`.

``langchain_ollama`` is an optional extra (``emonk[model-provider-ollama]``).
Tests inject a synthetic module so the provider's kwarg forwarding is
exercised without the real SDK or a running Ollama daemon.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from src.core.harness.extensions import ModelProvider
from src.core.harness.extensions.model_providers import OllamaProvider  # noqa: F401
from src.core.harness.specs import AgentSpec


class _FakeChatOllama:
    last_kwargs: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs


@pytest.fixture
def fake_ollama_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    _FakeChatOllama.last_kwargs = None
    fake = ModuleType("langchain_ollama")
    fake.ChatOllama = _FakeChatOllama  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langchain_ollama", fake)
    return fake


def test_mp_c_01_build_forwards_agent_spec_kwargs(fake_ollama_module: ModuleType) -> None:
    """MP-C-01: Ollama provider forwards model / temperature / num_predict / base_url."""
    provider = OllamaProvider(base_url="http://ollama.internal:11434")
    agent = AgentSpec(
        name="a",
        model="llama3.1",
        temperature=0.5,
        max_output_tokens=512,
    )

    provider.build(agent)

    assert _FakeChatOllama.last_kwargs is not None
    assert _FakeChatOllama.last_kwargs == {
        "model": "llama3.1",
        "temperature": 0.5,
        "num_predict": 512,
        "base_url": "http://ollama.internal:11434",
    }


def test_extra_model_kwargs_are_merged(fake_ollama_module: ModuleType) -> None:
    """AgentSpec.extra_model_kwargs pass through to ChatOllama unchanged."""
    provider = OllamaProvider()
    agent = AgentSpec(
        name="a",
        model="llama3.1",
        extra_model_kwargs={"num_ctx": 4096, "keep_alive": "5m"},
    )

    provider.build(agent)

    assert _FakeChatOllama.last_kwargs is not None
    assert _FakeChatOllama.last_kwargs["num_ctx"] == 4096
    assert _FakeChatOllama.last_kwargs["keep_alive"] == "5m"


def test_mp_c_02_capabilities_report_tool_calling() -> None:
    """MP-C-02: Ollama advertises tool calling (model-dependent)."""
    caps = OllamaProvider().capabilities()
    assert caps.tool_calling is True
    assert caps.streaming is True
    assert caps.max_context_tokens == 8_192


def test_registry_resolves_ollama_from_spec_dump() -> None:
    """Builtin registration satisfies the registry resolve path."""
    payload = {"backend": "ollama", "base_url": "http://localhost:11434"}
    resolved = ModelProvider.registry.resolve(payload)
    assert isinstance(resolved, OllamaProvider)
    assert resolved.base_url == "http://localhost:11434"
