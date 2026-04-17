"""MP-C-01 … MP-C-03 for :class:`BedrockProvider`.

``langchain_aws`` is an optional extra (``emonk[model-provider-bedrock]``).
Tests install a synthetic ``langchain_aws`` module into ``sys.modules``
before the provider's lazy import runs so the provider's Converse wiring
is exercised without requiring the real SDK in CI.

MP-C-03 (1B §11.5) is the Bedrock tool-use schema pin. The full schema
pin against a live ``boto3.client('bedrock-runtime').converse`` call is
descoped here (it requires the real ``langchain-aws`` package); instead
we assert the provider constructs ``ChatBedrockConverse`` with the
expected kwargs and forwards guardrail config when supplied. The live
schema pin runs under the integration marker once the extra is
installed.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from src.core.harness.extensions import ModelProvider
from src.core.harness.extensions.model_providers import BedrockProvider  # noqa: F401
from src.core.harness.specs import AgentSpec


class _FakeChatBedrockConverse:
    """Capture the kwargs :class:`BedrockProvider` hands to ``ChatBedrockConverse``."""

    last_kwargs: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs
        self.kwargs = kwargs

    def bind_tools(self, tools: list[Any]) -> _FakeChatBedrockConverse:  # noqa: D401
        """Record tool bindings; a real Converse call would register ``toolConfig``."""
        self.bound_tools = list(tools)
        return self


@pytest.fixture
def fake_bedrock_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Inject a synthetic ``langchain_aws`` module exposing the fake Converse class."""
    _FakeChatBedrockConverse.last_kwargs = None
    fake = ModuleType("langchain_aws")
    fake.ChatBedrockConverse = _FakeChatBedrockConverse  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langchain_aws", fake)
    return fake


def test_mp_c_01_build_forwards_agent_spec_kwargs(fake_bedrock_module: ModuleType) -> None:
    """MP-C-01: Bedrock provider forwards model_id / region / temperature / max_tokens."""
    provider = BedrockProvider(
        region="us-west-2",
        model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
    )
    agent = AgentSpec(
        name="a",
        model="ignored-when-model-id-present",
        temperature=0.2,
        max_output_tokens=4096,
    )

    provider.build(agent)

    assert _FakeChatBedrockConverse.last_kwargs is not None
    assert _FakeChatBedrockConverse.last_kwargs == {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "region_name": "us-west-2",
        "temperature": 0.2,
        "max_tokens": 4096,
    }


def test_model_id_falls_back_to_agent_model(fake_bedrock_module: ModuleType) -> None:
    """When ``spec.model_id`` is empty, ``agent_spec.model`` carries over (shim path)."""
    provider = BedrockProvider()
    agent = AgentSpec(name="a", model="anthropic.claude-legacy")

    provider.build(agent)

    assert _FakeChatBedrockConverse.last_kwargs is not None
    assert _FakeChatBedrockConverse.last_kwargs["model_id"] == "anthropic.claude-legacy"


def test_guardrail_config_forwarded_when_set(fake_bedrock_module: ModuleType) -> None:
    """Guardrail id/version are packaged into the SDK ``guardrail_config`` kwarg."""
    provider = BedrockProvider(
        model_id="anthropic.claude-3-5-sonnet",
        guardrail_id="gr-abc",
        guardrail_version="1",
    )
    agent = AgentSpec(name="a")

    provider.build(agent)

    assert _FakeChatBedrockConverse.last_kwargs is not None
    assert _FakeChatBedrockConverse.last_kwargs["guardrail_config"] == {
        "guardrailIdentifier": "gr-abc",
        "guardrailVersion": "1",
    }


def test_guardrail_version_defaults_to_draft(fake_bedrock_module: ModuleType) -> None:
    """When only guardrail_id is supplied the SDK expects a version — default to DRAFT."""
    provider = BedrockProvider(model_id="anthropic.claude-3", guardrail_id="gr-xyz")
    agent = AgentSpec(name="a")

    provider.build(agent)

    assert _FakeChatBedrockConverse.last_kwargs is not None
    assert (
        _FakeChatBedrockConverse.last_kwargs["guardrail_config"]["guardrailVersion"] == "DRAFT"
    )


def test_mp_c_02_capabilities_report_tool_calling() -> None:
    """MP-C-02: Bedrock advertises tool calling + streaming."""
    caps = BedrockProvider().capabilities()
    assert caps.tool_calling is True
    assert caps.streaming is True
    assert caps.max_context_tokens == 200_000


def test_mp_c_03_tool_binding_surface(fake_bedrock_module: ModuleType) -> None:
    """MP-C-03 (descoped): the returned chat model exposes ``bind_tools``.

    A full schema pin against a live boto3 ``Converse`` request requires
    the real ``langchain-aws`` package — that test runs under the
    integration marker once the optional extra is installed. The
    in-process contract here verifies the surface the assembler
    depends on is preserved.
    """
    provider = BedrockProvider(model_id="anthropic.claude-3-5-sonnet")
    agent = AgentSpec(name="a")

    chat = provider.build(agent)

    assert hasattr(chat, "bind_tools")
    bound = chat.bind_tools(
        [
            {
                "name": "echo",
                "description": "echoes",
                "input_schema": {"type": "object", "properties": {"msg": {"type": "string"}}},
            }
        ]
    )
    assert isinstance(bound, _FakeChatBedrockConverse)
    assert bound.bound_tools[0]["name"] == "echo"
    assert "input_schema" in bound.bound_tools[0]


def test_registry_resolves_bedrock_from_spec_dump(fake_bedrock_module: ModuleType) -> None:
    """The builtin registration lets the assembler resolve BedrockProvider."""
    payload = {
        "backend": "bedrock",
        "region": "us-west-2",
        "model_id": "anthropic.claude-3-5-sonnet",
        "guardrail_id": None,
        "guardrail_version": None,
    }
    resolved = ModelProvider.registry.resolve(payload)
    assert isinstance(resolved, BedrockProvider)
    assert resolved.region == "us-west-2"
    assert resolved.model_id == "anthropic.claude-3-5-sonnet"
