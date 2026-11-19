"""model_capabilities.supports_param: prefix lookup, defaults, precedence."""

from __future__ import annotations

from types import SimpleNamespace

from monkeybot.core.llm.provider import supports_output_cap_override
from monkeybot.providers.model_capabilities import max_output_tokens, supports_param


def test_unknown_model_supports_everything() -> None:
    assert supports_param("claude-3-5-sonnet-20241022", "temperature") is True
    assert supports_param("claude-3-5-sonnet-20241022", "thinking") is True


def test_known_blocked_model_rejects_temperature() -> None:
    assert supports_param("claude-sonnet-5", "temperature") is False
    assert supports_param("claude-sonnet-5-20260101", "temperature") is False
    assert supports_param("claude-haiku-5-5", "temperature") is False


def test_known_blocked_model_rejects_manual_thinking() -> None:
    assert supports_param("claude-opus-4-7-20260101", "thinking") is False


def test_known_blocked_model_still_unaffected_for_other_params() -> None:
    assert supports_param("claude-sonnet-5", "some_future_param") is True


def test_longest_prefix_wins() -> None:
    assert supports_param("claude-opus-4-7", "temperature") is False
    assert supports_param("claude-opus-4", "temperature") is True


def test_bedrock_namespaced_ids_match() -> None:
    assert supports_param("anthropic.claude-sonnet-5-20260101-v1:0", "temperature") is False
    assert supports_param("us.anthropic.claude-sonnet-5-20260101-v1:0", "temperature") is False
    assert supports_param("eu.anthropic.claude-opus-4-7-20260101-v1:0", "thinking") is False
    # Older Bedrock models keep their knobs.
    assert supports_param("us.anthropic.claude-sonnet-4-20250514-v1:0", "temperature") is True


def test_vertex_suffixed_ids_match() -> None:
    assert supports_param("claude-sonnet-5@20260101", "temperature") is False


def test_max_output_tokens_known_prefixes() -> None:
    assert max_output_tokens("us.zai.glm-5.3") == 128_000
    assert max_output_tokens("global.anthropic.claude-opus-5-5") == 128_000
    assert max_output_tokens("claude-haiku-4-5@20251001") == 64_000
    assert max_output_tokens("us.meta.llama4-maverick") is None


def test_output_cap_override_is_limited_to_three_providers() -> None:
    assert supports_output_cap_override(SimpleNamespace(name="bedrock")) is True
    assert supports_output_cap_override(SimpleNamespace(name="claude")) is True
    assert supports_output_cap_override(SimpleNamespace(name="vertex-claude")) is True
    assert supports_output_cap_override(SimpleNamespace(name="fake")) is False
    assert supports_output_cap_override(SimpleNamespace(name="gemini")) is False
    assert supports_output_cap_override(SimpleNamespace(name="openai")) is False


def test_xai_grok_rejects_temperature() -> None:
    assert supports_param("us.xai.grok-4.6", "temperature") is False
    assert supports_param("xai.grok-4.6", "temperature") is False
    assert supports_param("bedrock/us.xai.grok-4.6", "temperature") is False
    assert supports_param("us.amazon.nova-pro-v1:0", "temperature") is True
