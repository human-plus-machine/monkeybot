"""Assembler backward-compat shim for ``HarnessConfig.model_provider`` (Story 7).

Verifies the 1B §4.3 contract: when ``HarnessConfig.model_provider`` is
``None`` the assembler synthesizes a :class:`ModelProviderSpec` from
the legacy ``AgentSpec.provider`` literal, so pre-Story-7 configs keep
working. When ``HarnessConfig.model_provider`` is set, it overrides the
legacy field.
"""

from __future__ import annotations

from src.core.harness.assembler import (
    _build_model_provider,
    _synthesize_model_provider_spec,
)
from src.core.harness.extensions.model_providers import (
    AnthropicProvider,
    BedrockProvider,
    OpenAIProvider,
    VertexProvider,
)
from src.core.harness.extensions.specs.model_provider import (
    ModelProviderAnthropicSpec,
    ModelProviderBedrockSpec,
    ModelProviderOpenAISpec,
    ModelProviderVertexSpec,
)
from src.core.harness.specs import AgentSpec, HarnessConfig


def _cfg(provider: str, *, model: str = "gemini-2.5-flash") -> HarnessConfig:
    return HarnessConfig(agent=AgentSpec(name="x", provider=provider, model=model))


def test_shim_synthesizes_vertex_spec_for_google_vertexai() -> None:
    """Legacy ``provider='google_vertexai'`` + ``model_provider=None`` → VertexSpec."""
    cfg = _cfg("google_vertexai")
    assert cfg.model_provider is None

    spec = _synthesize_model_provider_spec(cfg)

    assert isinstance(spec, ModelProviderVertexSpec)
    assert spec.backend == "vertex"


def test_shim_synthesizes_bedrock_spec_with_model_id() -> None:
    """``provider='bedrock'`` → BedrockSpec with ``model_id`` carried from AgentSpec.model."""
    cfg = _cfg("bedrock", model="anthropic.claude-3-5-sonnet-20241022-v2:0")

    spec = _synthesize_model_provider_spec(cfg)

    assert isinstance(spec, ModelProviderBedrockSpec)
    assert spec.model_id == "anthropic.claude-3-5-sonnet-20241022-v2:0"


def test_shim_synthesizes_openai_spec() -> None:
    """``provider='openai'`` → OpenAISpec (API key comes from env at build time)."""
    cfg = _cfg("openai", model="gpt-4o-mini")

    spec = _synthesize_model_provider_spec(cfg)

    assert isinstance(spec, ModelProviderOpenAISpec)


def test_shim_synthesizes_anthropic_spec() -> None:
    """``provider='anthropic'`` → AnthropicSpec."""
    cfg = _cfg("anthropic", model="claude-3-5-sonnet-20241022")

    spec = _synthesize_model_provider_spec(cfg)

    assert isinstance(spec, ModelProviderAnthropicSpec)


def test_explicit_model_provider_overrides_legacy_agent_provider() -> None:
    """When ``HarnessConfig.model_provider`` is set, it wins over ``agent.provider``."""
    bedrock = ModelProviderBedrockSpec(model_id="anthropic.claude-3-5-sonnet")
    cfg = HarnessConfig(
        agent=AgentSpec(name="x", provider="google_vertexai"),
        model_provider=bedrock,
    )

    spec = _synthesize_model_provider_spec(cfg)

    assert spec is bedrock


def test_build_resolves_vertex_provider_for_legacy_config() -> None:
    """End-to-end: legacy vertex config routes through the registry to VertexProvider."""
    cfg = _cfg("google_vertexai")

    instance = _build_model_provider(cfg)

    assert isinstance(instance, VertexProvider)


def test_build_resolves_bedrock_provider_for_legacy_bedrock_config() -> None:
    """End-to-end: ``provider='bedrock'`` resolves to a BedrockProvider instance."""
    cfg = _cfg("bedrock", model="anthropic.claude-3-5-sonnet")

    instance = _build_model_provider(cfg)

    assert isinstance(instance, BedrockProvider)
    assert instance.model_id == "anthropic.claude-3-5-sonnet"


def test_build_resolves_openai_provider_for_legacy_openai_config() -> None:
    """End-to-end: ``provider='openai'`` resolves to an OpenAIProvider instance."""
    cfg = _cfg("openai", model="gpt-4o-mini")

    instance = _build_model_provider(cfg)

    assert isinstance(instance, OpenAIProvider)


def test_build_resolves_anthropic_provider_for_legacy_anthropic_config() -> None:
    """End-to-end: ``provider='anthropic'`` resolves to an AnthropicProvider instance."""
    cfg = _cfg("anthropic", model="claude-3-5-sonnet")

    instance = _build_model_provider(cfg)

    assert isinstance(instance, AnthropicProvider)


def test_build_resolves_explicit_bedrock_spec_overrides_agent_provider() -> None:
    """Explicit ``model_provider`` overrides legacy ``agent.provider`` at the assembler layer."""
    cfg = HarnessConfig(
        agent=AgentSpec(name="x", provider="google_vertexai"),
        model_provider=ModelProviderBedrockSpec(model_id="anthropic.claude-3-5-sonnet"),
    )

    instance = _build_model_provider(cfg)

    assert isinstance(instance, BedrockProvider)


def test_vertex_anthropic_legacy_provider_falls_back_to_vertex_spec() -> None:
    """``provider='vertex_anthropic'`` has no dedicated backend yet — shim falls back to Vertex."""
    cfg = _cfg("vertex_anthropic")

    spec = _synthesize_model_provider_spec(cfg)

    assert isinstance(spec, ModelProviderVertexSpec)
