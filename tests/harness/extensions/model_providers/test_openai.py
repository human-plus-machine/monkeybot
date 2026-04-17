"""MP-C-01 / MP-C-02 for :class:`OpenAIProvider`.

``langchain_openai`` is an optional extra (``emonk[model-provider-openai]``).
Tests inject a synthetic ``langchain_openai`` module so the provider's
kwarg forwarding can be exercised without the real SDK.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from src.core.harness.extensions import ModelProvider
from src.core.harness.extensions.model_providers import OpenAIProvider  # noqa: F401
from src.core.harness.specs import AgentSpec


class _FakeChatOpenAI:
    last_kwargs: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs


@pytest.fixture
def fake_openai_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    _FakeChatOpenAI.last_kwargs = None
    fake = ModuleType("langchain_openai")
    fake.ChatOpenAI = _FakeChatOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langchain_openai", fake)
    return fake


def test_mp_c_01_build_forwards_agent_spec_kwargs(
    fake_openai_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MP-C-01: OpenAI provider forwards model / temperature / max_tokens."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    provider = OpenAIProvider()
    agent = AgentSpec(name="a", model="gpt-4o-mini", temperature=0.1, max_output_tokens=2048)

    provider.build(agent)

    assert _FakeChatOpenAI.last_kwargs is not None
    assert _FakeChatOpenAI.last_kwargs["model"] == "gpt-4o-mini"
    assert _FakeChatOpenAI.last_kwargs["temperature"] == 0.1
    assert _FakeChatOpenAI.last_kwargs["max_tokens"] == 2048
    api_key = _FakeChatOpenAI.last_kwargs["api_key"]
    assert api_key.get_secret_value() == "sk-test-123"


def test_api_key_omitted_when_env_missing(
    fake_openai_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the env var is unset the SDK default resolution takes over."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIProvider()
    agent = AgentSpec(name="a", model="gpt-4o")

    provider.build(agent)

    assert _FakeChatOpenAI.last_kwargs is not None
    assert "api_key" not in _FakeChatOpenAI.last_kwargs


def test_custom_api_key_handle_is_honoured(
    fake_openai_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-default ``api_key_handle`` routes the lookup to a different env var."""
    monkeypatch.setenv("CUSTOM_OPENAI_KEY", "sk-custom")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIProvider(api_key_handle="CUSTOM_OPENAI_KEY")
    agent = AgentSpec(name="a", model="gpt-4o")

    provider.build(agent)

    assert _FakeChatOpenAI.last_kwargs is not None
    assert _FakeChatOpenAI.last_kwargs["api_key"].get_secret_value() == "sk-custom"


def test_mp_c_02_capabilities_report_tool_calling_and_vision() -> None:
    """MP-C-02: OpenAI advertises tool calling + vision (GPT-4o class)."""
    caps = OpenAIProvider().capabilities()
    assert caps.tool_calling is True
    assert caps.vision is True
    assert caps.max_context_tokens == 128_000


def test_registry_resolves_openai_from_spec_dump() -> None:
    """Builtin registration satisfies the registry resolve path."""
    payload = {"backend": "openai", "api_key_handle": "OPENAI_API_KEY"}
    resolved = ModelProvider.registry.resolve(payload)
    assert isinstance(resolved, OpenAIProvider)
    assert resolved.api_key_handle == "OPENAI_API_KEY"
