"""Anthropic Claude provider (direct API, async streaming)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from monkeybot.core.llm.provider import Message, ProviderEvent
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._utils import (
    anthropic_tool_defs,
    build_anthropic_messages,
    build_cached_system_blocks,
    count_anthropic_input_tokens,
    iter_anthropic_sdk_stream,
    mark_last_tool_cached,
    split_leading_system,
)
from monkeybot.providers.sampling import resolve_model_sampling


class ClaudeProvider:
    """Anthropic Claude using the ``anthropic`` SDK."""

    @property
    def name(self) -> str:
        return "claude"

    @property
    def supports_streaming(self) -> bool:
        return True

    def __init__(
        self,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        cache_enabled: bool = True,
    ) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY is not set")
        self._cache_enabled = cache_enabled
        sampling = resolve_model_sampling(temperature=temperature, max_tokens=max_tokens)
        self._temperature = sampling.temperature
        self._max_tokens = sampling.max_tokens

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
        return await count_anthropic_input_tokens(
            anthropic.AsyncAnthropic(),
            anthropic_module=anthropic,
            model=model,
            system=system,
            messages=cast(Any, converted_messages),
            tools=anthropic_tool_defs(tools) if tools else None,
        )

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        import anthropic  # noqa: PLC0415

        system, msgs = split_leading_system(messages)
        converted_messages = build_anthropic_messages(msgs)
        converted = anthropic_tool_defs(tools) if tools else None
        if self._cache_enabled and system:
            system_param: Any = build_cached_system_blocks(system)
        else:
            system_param = system or anthropic.NOT_GIVEN

        if self._cache_enabled and converted:
            tools_param: Any = mark_last_tool_cached(converted)
        else:
            tools_param = converted if converted else anthropic.NOT_GIVEN

        stream_kwargs: dict[str, Any] = {
            "model": model,
            "system": system_param,
            "messages": cast(Any, converted_messages),
            "tools": tools_param,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        if thinking_budget is not None and thinking_budget > 0:
            stream_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            }
            # Anthropic requires temperature=1 when extended thinking is enabled.
            stream_kwargs["temperature"] = 1

        client = anthropic.AsyncAnthropic()
        async for event in iter_anthropic_sdk_stream(
            client,
            stream_kwargs,
            provider="claude",
            error_message="Claude stream error: %s",
            n_messages=len(messages),
            n_tools=len(tools),
        ):
            yield event
