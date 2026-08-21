"""Unified model sampling configuration across providers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from monkeybot.core.config.settings import get_provider_config
from monkeybot.core.llm.provider import Message
from monkeybot.providers.bedrock import BedrockProvider
from monkeybot.providers.claude import ClaudeProvider
from monkeybot.providers.gemini import GeminiProvider
from monkeybot.providers.openai import OpenAIProvider
from monkeybot.providers.sampling import (
    DEFAULT_MODEL_MAX_TOKENS,
    DEFAULT_MODEL_TEMPERATURE,
    resolve_model_sampling,
)
from monkeybot.providers.vertex_claude import VertexClaudeProvider
from tests.providers.conftest import make_anthropic_stream_mock


class TestResolveModelSampling:
    def test_defaults_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODEL_TEMPERATURE", raising=False)
        monkeypatch.delenv("MODEL_MAX_TOKENS", raising=False)
        sampling = resolve_model_sampling()
        assert sampling.temperature == DEFAULT_MODEL_TEMPERATURE
        assert sampling.max_tokens == DEFAULT_MODEL_MAX_TOKENS

    def test_explicit_args_override_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODEL_TEMPERATURE", "0.9")
        monkeypatch.setenv("MODEL_MAX_TOKENS", "1234")
        sampling = resolve_model_sampling(temperature=0.2, max_tokens=8192)
        assert sampling.temperature == 0.2
        assert sampling.max_tokens == 8192

    def test_reads_from_env_when_args_omitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODEL_TEMPERATURE", "0.15")
        monkeypatch.setenv("MODEL_MAX_TOKENS", "4096")
        sampling = resolve_model_sampling()
        assert sampling.temperature == 0.15
        assert sampling.max_tokens == 4096


class TestProviderSamplingConstructors:
    def test_claude_stores_resolved_sampling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        provider = ClaudeProvider(temperature=0.2, max_tokens=8192)
        assert provider._temperature == 0.2
        assert provider._max_tokens == 8192

    def test_openai_stores_resolved_sampling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        provider = OpenAIProvider(temperature=0.2, max_tokens=8192)
        assert provider._temperature == 0.2
        assert provider._max_tokens == 8192

    def test_bedrock_stores_resolved_sampling(self) -> None:
        provider = BedrockProvider(temperature=0.2, max_tokens=8192)
        assert provider._temperature == 0.2
        assert provider._max_tokens == 8192

    def test_vertex_stores_resolved_sampling(self) -> None:
        provider = VertexClaudeProvider(project_id="p", temperature=0.2, max_tokens=8192)
        assert provider._temperature == 0.2
        assert provider._max_tokens == 8192

    def test_gemini_max_tokens_alias(self) -> None:
        provider = GeminiProvider(temperature=0.2, max_tokens=8192)
        assert provider._temperature == 0.2
        assert provider._max_tokens == 8192

    def test_gemini_max_output_tokens_backward_compat(self) -> None:
        provider = GeminiProvider(temperature=0.2, max_output_tokens=8192)
        assert provider._max_tokens == 8192

    def test_gemini_rejects_conflicting_max_token_args(self) -> None:
        with pytest.raises(ValueError, match="only one of max_tokens or max_output_tokens"):
            GeminiProvider(max_tokens=100, max_output_tokens=200)


class TestGetProviderConfigSampling:
    def test_bedrock_threads_sampling_from_yaml_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_instance = SimpleNamespace(name="bedrock")
        with patch(
            "monkeybot.providers.bedrock.BedrockProvider",
            return_value=mock_instance,
        ) as mock_cls:
            cfg = get_provider_config(
                provider="aws_bedrock",
                model_name="claude-sonnet-4",
                temperature=0.2,
                max_tokens=8192,
            )
            assert cfg.provider is mock_instance
            mock_cls.assert_called_once_with(
                aws_region=None,
                temperature=0.2,
                max_tokens=8192,
            )

    def test_anthropic_threads_sampling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        cfg = get_provider_config(
            provider="anthropic",
            model_name="claude-3-5-sonnet-20241022",
            temperature=0.2,
            max_tokens=8192,
        )
        assert isinstance(cfg.provider, ClaudeProvider)
        assert cfg.provider._temperature == 0.2
        assert cfg.provider._max_tokens == 8192


@pytest.mark.asyncio
async def test_bedrock_stream_passes_sampling_to_api() -> None:
    provider = BedrockProvider(temperature=0.2, max_tokens=8192, aws_region="us-east-1")
    events = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(usage=SimpleNamespace(input_tokens=1)),
        ),
        SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=1)),
    ]
    client = make_anthropic_stream_mock(events)
    provider._client = lambda: client  # type: ignore[method-assign]

    async for _ in provider.stream([Message.text("user", "hi")], [], model="claude-3-5-sonnet-20241022"):
        pass

    kwargs = client.messages.stream.call_args.kwargs
    assert kwargs["temperature"] == 0.2
    assert kwargs["max_tokens"] == 8192
