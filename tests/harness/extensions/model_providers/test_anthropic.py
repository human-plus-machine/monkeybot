"""MP-C-01 / MP-C-02 for :class:`AnthropicProvider`.

``langchain_anthropic`` is an optional extra
(``emonk[model-provider-anthropic]``). Tests inject a synthetic module
so the provider's kwarg forwarding is exercised without the real SDK.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from src.core.harness.extensions import ModelProvider
from src.core.harness.extensions.model_providers import AnthropicProvider  # noqa: F401
from src.core.harness.specs import AgentSpec


class _FakeChatAnthropic:
    last_kwargs: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs


@pytest.fixture
def fake_anthropic_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    _FakeChatAnthropic.last_kwargs = None
    fake = ModuleType("langchain_anthropic")
    fake.ChatAnthropic = _FakeChatAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langchain_anthropic", fake)
    return fake


def test_mp_c_01_build_forwards_agent_spec_kwargs(
    fake_anthropic_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MP-C-01: Anthropic provider forwards model / temperature / max_tokens / api_key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    provider = AnthropicProvider()
    agent = AgentSpec(
        name="a",
        model="claude-3-5-sonnet-20241022",
        temperature=0.4,
        max_output_tokens=3000,
    )

    provider.build(agent)

    assert _FakeChatAnthropic.last_kwargs is not None
    assert _FakeChatAnthropic.last_kwargs["model"] == "claude-3-5-sonnet-20241022"
    assert _FakeChatAnthropic.last_kwargs["temperature"] == 0.4
    assert _FakeChatAnthropic.last_kwargs["max_tokens"] == 3000
    assert _FakeChatAnthropic.last_kwargs["api_key"].get_secret_value() == "sk-ant-test"


def test_api_key_omitted_when_env_missing(
    fake_anthropic_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing env var leaves the SDK to do its own default resolution."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = AnthropicProvider()
    agent = AgentSpec(name="a", model="claude-3-5-sonnet")

    provider.build(agent)

    assert _FakeChatAnthropic.last_kwargs is not None
    assert "api_key" not in _FakeChatAnthropic.last_kwargs


def test_mp_c_02_capabilities_report_tool_calling_and_vision() -> None:
    """MP-C-02: Anthropic advertises tool calling + vision + 200K context."""
    caps = AnthropicProvider().capabilities()
    assert caps.tool_calling is True
    assert caps.vision is True
    assert caps.max_context_tokens == 200_000


def test_registry_resolves_anthropic_from_spec_dump() -> None:
    """Builtin registration satisfies the registry resolve path."""
    payload = {"backend": "anthropic", "api_key_handle": "ANTHROPIC_API_KEY"}
    resolved = ModelProvider.registry.resolve(payload)
    assert isinstance(resolved, AnthropicProvider)
    assert resolved.api_key_handle == "ANTHROPIC_API_KEY"
