"""Anthropic Claude on AWS Bedrock (``AsyncAnthropicBedrock``)."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.llm.provider import Message, ProviderCallHints, ProviderEvent
from monkeybot.core.logging_utils import kv
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._utils import (
    anthropic_tool_defs,
    build_anthropic_messages,
    build_cached_system_blocks,
    count_anthropic_input_tokens,
    estimate_anthropic_input_tokens,
    iter_anthropic_sdk_stream,
    mark_last_tool_cached,
    split_leading_system,
)
from monkeybot.providers.sampling import resolve_model_sampling

_log = logging.getLogger(__name__)


class BedrockClaudeProvider:
    """Claude on AWS Bedrock via the Anthropic Bedrock client."""

    @property
    def name(self) -> str:
        return "bedrock"

    @property
    def supports_streaming(self) -> bool:
        return True

    def __init__(
        self,
        *,
        aws_region: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self._aws_region = (
            (aws_region or "").strip()
            or os.environ.get("AWS_REGION", "").strip()
            or os.environ.get("AWS_DEFAULT_REGION", "").strip()
            or "us-east-1"
        )
        sampling = resolve_model_sampling(temperature=temperature, max_tokens=max_tokens)
        self._temperature = sampling.temperature
        self._max_tokens = sampling.max_tokens

    def _client(self) -> Any:
        from anthropic import AsyncAnthropicBedrock  # noqa: PLC0415

        return AsyncAnthropicBedrock(aws_region=self._aws_region)

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> int:
        del thinking_budget
        import anthropic  # noqa: PLC0415

        system, msgs = split_leading_system(messages)
        converted_messages = build_anthropic_messages(msgs)
        converted_tools = anthropic_tool_defs(tools) if tools else None
        client = self._client()
        try:
            return await count_anthropic_input_tokens(
                client,
                anthropic_module=anthropic,
                model=model,
                system=system,
                messages=converted_messages,
                tools=converted_tools,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "token counting" in msg or "not supported" in msg or "bedrock" in msg:
                _log.warning(
                    "Bedrock count_tokens unavailable, using estimate %s",
                    kv(provider="bedrock", model=model),
                    exc_info=True,
                )
                return estimate_anthropic_input_tokens(
                    system=system,
                    messages=converted_messages,
                    tools=converted_tools,
                )
            raise

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        hints: ProviderCallHints | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del thinking_budget
        import anthropic  # noqa: PLC0415

        retention = hints.cache_retention if hints is not None else "short"
        system, msgs = split_leading_system(messages)
        converted_messages = build_anthropic_messages(msgs)
        converted = anthropic_tool_defs(tools) if tools else None
        system_param: Any = (
            build_cached_system_blocks(system, cache_retention=retention)
            if system
            else anthropic.NOT_GIVEN
        )
        tools_param: Any = (
            mark_last_tool_cached(converted, cache_retention=retention)
            if converted
            else anthropic.NOT_GIVEN
        )

        client = self._client()
        stream_kwargs: dict[str, Any] = {
            "model": model,
            "system": system_param,
            "messages": converted_messages,
            "tools": tools_param,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        async for event in iter_anthropic_sdk_stream(
            client,
            stream_kwargs,
            provider="bedrock",
            error_message="Bedrock Claude stream error: %s",
            n_messages=len(messages),
            n_tools=len(tools),
        ):
            yield event
