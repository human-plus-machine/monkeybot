"""Unified model sampling configuration across providers."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from monkeybot.core.config.settings import get_provider_config
from monkeybot.core.llm.provider import Message
from monkeybot.providers._openai_compat import drop_rejected_sampling
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

    def test_reasoning_effort_unset_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODEL_REASONING_EFFORT", raising=False)
        sampling = resolve_model_sampling()
        assert sampling.reasoning_effort is None

    def test_reasoning_effort_normalizes_and_explicit_empty_skips_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODEL_REASONING_EFFORT", "max")
        assert resolve_model_sampling(reasoning_effort="  XHIGH ").reasoning_effort == "xhigh"
        assert resolve_model_sampling(reasoning_effort="").reasoning_effort is None

    def test_reasoning_effort_from_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from monkeybot.core.config.snapshot import build_runtime_config

        monkeypatch.chdir(tmp_path)
        cfg_dir = tmp_path / "monkeybot_config"
        cfg_dir.mkdir()
        (cfg_dir / "monkeybot.yaml").write_text(
            "model:\n  reasoning_effort: high\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("MODEL_REASONING_EFFORT", "low")
        cfg = build_runtime_config(agent_root=tmp_path)
        sampling = resolve_model_sampling(config=cfg)
        assert sampling.reasoning_effort == "high"

    def test_reads_from_env_when_args_omitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODEL_TEMPERATURE", "0.15")
        monkeypatch.setenv("MODEL_MAX_TOKENS", "4096")
        sampling = resolve_model_sampling()
        assert sampling.temperature == 0.15
        assert sampling.max_tokens == 4096

    def test_snapshot_beats_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from monkeybot.core.config.snapshot import build_runtime_config

        monkeypatch.chdir(tmp_path)
        cfg_dir = tmp_path / "monkeybot_config"
        cfg_dir.mkdir()
        (cfg_dir / "monkeybot.yaml").write_text(
            "model:\n  temperature: 0.3\n  max_tokens: 1111\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("MODEL_TEMPERATURE", raising=False)
        monkeypatch.delenv("MODEL_MAX_TOKENS", raising=False)
        cfg = build_runtime_config(agent_root=tmp_path)
        monkeypatch.setenv("MODEL_TEMPERATURE", "0.9")
        monkeypatch.setenv("MODEL_MAX_TOKENS", "9999")
        sampling = resolve_model_sampling(config=cfg)
        assert sampling.temperature == 0.3
        assert sampling.max_tokens == 1111


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
    def test_bedrock_threads_sampling_from_yaml_params(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
                reasoning_effort="",
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
        assert cfg.provider._reasoning_effort is None


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

    async for _ in provider.stream(
        [Message.text("user", "hi")], [], model="claude-3-5-sonnet-20241022"
    ):
        pass

    kwargs = client.messages.stream.call_args.kwargs
    assert kwargs["temperature"] == 0.2
    assert kwargs["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_bedrock_claude_sends_reasoning_effort() -> None:
    provider = BedrockProvider(
        temperature=0.2,
        max_tokens=8192,
        aws_region="us-east-1",
        reasoning_effort="high",
    )
    events = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(usage=SimpleNamespace(input_tokens=1)),
        ),
        SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=1)),
    ]
    client = make_anthropic_stream_mock(events)
    provider._client = lambda: client  # type: ignore[method-assign]

    async for _ in provider.stream(
        [Message.text("user", "hi")], [], model="claude-3-5-sonnet-20241022"
    ):
        pass

    kwargs = client.messages.stream.call_args.kwargs
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "high"}
    assert "temperature" not in kwargs


def test_drop_rejected_sampling_quotes_openai_style() -> None:
    kwargs = {"temperature": 0.7, "reasoning_effort": "max", "model": "gpt-5"}
    dropped = drop_rejected_sampling(
        kwargs,
        RuntimeError("Error code: 400 - Unsupported parameter: 'temperature'"),
    )
    assert dropped == ["temperature"]
    assert "temperature" not in kwargs
    assert kwargs["reasoning_effort"] == "max"
    assert drop_rejected_sampling(kwargs, RuntimeError("Error code: 500")) == []


def test_drop_rejected_sampling_ignores_bare_mentions_and_token_counts() -> None:
    kwargs = {"temperature": 0.7, "reasoning_effort": "max"}
    untouched = drop_rejected_sampling(
        kwargs,
        RuntimeError("Error code: 400 - max_tokens must be at most 4000 and temperature was fine"),
    )
    assert untouched == []
    assert kwargs["temperature"] == 0.7
    assert (
        drop_rejected_sampling(
            kwargs,
            RuntimeError("max_tokens 4000 exceeds the model limit"),
        )
        == []
    )


@pytest.mark.asyncio
async def test_openai_retries_then_omits_rejected_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    from monkeybot.core.llm.provider import TextDelta

    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.delenv("MODEL_REASONING_EFFORT", raising=False)
    calls: list[dict[str, Any]] = []

    async def fake_iter(_client: object, kwargs: dict[str, Any], **_rest: object):
        calls.append(dict(kwargs))
        if "reasoning_effort" in kwargs:
            raise RuntimeError("Error code: 400 - Unsupported parameter: 'reasoning_effort'")
        yield TextDelta(text="ok")

    async def fake_messages(_messages: list[Message]) -> tuple[None, list[dict[str, str]]]:
        return None, [{"role": "user", "content": "hi"}]

    fake_openai = ModuleType("openai")
    fake_openai.AsyncOpenAI = lambda **_kwargs: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    monkeypatch.setattr("monkeybot.providers.openai.iter_openai_compat_stream", fake_iter)
    monkeypatch.setattr("monkeybot.providers.openai.messages_to_openai", fake_messages)

    provider = OpenAIProvider(reasoning_effort="max")
    first = [
        event async for event in provider.stream([Message.text("user", "hi")], [], model="gpt-4.1")
    ]
    assert [event.text for event in first if isinstance(event, TextDelta)] == ["ok"]
    assert calls[0]["reasoning_effort"] == "max"
    assert "reasoning_effort" not in calls[1]

    calls.clear()
    _ = [
        event async for event in provider.stream([Message.text("user", "hi")], [], model="gpt-4.1")
    ]
    assert len(calls) == 1
    assert "reasoning_effort" not in calls[0]


def test_rejected_sampling_is_omitted_on_the_next_request() -> None:
    from monkeybot.providers.rejected_sampling import (
        clear_rejected_sampling,
        omit_known_rejected,
    )

    clear_rejected_sampling()
    kwargs = {"temperature": 0.7, "reasoning_effort": "max"}
    dropped = drop_rejected_sampling(
        kwargs,
        RuntimeError("Error code: 400 - Unsupported parameter: 'reasoning_effort'"),
        provider="openai",
        model="gpt-remember-effort",
    )
    assert dropped == ["reasoning_effort"]
    again = {"temperature": 0.7, "reasoning_effort": "max"}
    omit_known_rejected("openai", "gpt-remember-effort", again)
    assert "reasoning_effort" not in again
    assert again["temperature"] == 0.7
    clear_rejected_sampling()


def test_rejected_sampling_retries_a_changed_value() -> None:
    from monkeybot.providers.rejected_sampling import omit_known_rejected

    drop_rejected_sampling(
        {"reasoning_effort": "max"},
        RuntimeError("Error code: 400 - Unsupported parameter: 'reasoning_effort'"),
        provider="openai",
        model="gpt-changed-effort",
    )
    changed = {"reasoning_effort": "high"}
    omit_known_rejected("openai", "gpt-changed-effort", changed)
    assert changed == {"reasoning_effort": "high"}


def test_openai_reasoning_effort_maps_off_words_to_none() -> None:
    from monkeybot.providers._openai_compat import openai_reasoning_effort

    assert openai_reasoning_effort("off") == "none"
    assert openai_reasoning_effort("disabled") == "none"
    assert openai_reasoning_effort("high") == "high"
    assert openai_reasoning_effort(None) is None


def test_compaction_summary_call_is_auxiliary() -> None:
    from monkeybot.core.runtime.history_compaction import _COMPACTION_SUMMARY_SYSTEM
    from monkeybot.core.types.content_blocks import Text
    from monkeybot.providers._utils import suppress_thinking_for_auxiliary_call

    messages = [
        Message(role="system", content=[Text(text=_COMPACTION_SUMMARY_SYSTEM)]),
        Message.text("user", "history"),
    ]
    assert suppress_thinking_for_auxiliary_call(messages, [])


def test_anthropic_effort_falls_back_to_temperature_after_rejection() -> None:
    from monkeybot.providers._utils import anthropic_sampling_kwargs
    from monkeybot.providers.rejected_sampling import remember_rejected_sampling

    model = "claude-3-5-sonnet-20241022"
    remember_rejected_sampling("claude", model, {"output_config": {"effort": "high"}})
    kwargs = anthropic_sampling_kwargs(
        model=model,
        temperature=0.2,
        thinking_budget=None,
        reasoning_effort="high",
        provider="claude",
    )
    assert "output_config" not in kwargs
    assert kwargs.get("temperature") == 0.2
