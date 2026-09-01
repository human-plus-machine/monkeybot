"""Anthropic Claude provider (direct API, async streaming)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from monkeybot.core.llm.provider import Message, ProviderCallHints, ProviderEvent
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._utils import (
    anthropic_tool_defs,
    build_anthropic_messages,
    count_anthropic_input_tokens,
    iter_anthropic_sdk_stream,
    prepare_anthropic_cached_payload,
    split_leading_system,
)
from monkeybot.providers.model_capabilities import supports_param
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
    ) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY is not set")
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
        hints: ProviderCallHints | None = None,
    ) -> int:
        del thinking_budget, hints
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
        hints: ProviderCallHints | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        import anthropic  # noqa: PLC0415

        retention = hints.cache_retention if hints is not None else "short"
        session_id = hints.session_id if hints is not None else None

        system, msgs = split_leading_system(messages)
        system_param, converted_messages, tools_param = prepare_anthropic_cached_payload(
            system=system,
            messages=msgs,
            tools=tools,
            cache_retention=retention,
            not_given=anthropic.NOT_GIVEN,
        )

        stream_kwargs: dict[str, Any] = {
            "model": model,
            "system": system_param,
            "messages": cast(Any, converted_messages),
            "tools": tools_param,
            "max_tokens": self._max_tokens,
        }
        if supports_param(model, "temperature"):
            stream_kwargs["temperature"] = self._temperature
        if (
            thinking_budget is not None
            and thinking_budget > 0
            and supports_param(model, "thinking")
        ):
            stream_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            }
            # Anthropic requires temperature=1 when extended thinking is enabled —
            # but only send it to models that accept a temperature at all.
            if supports_param(model, "temperature"):
                stream_kwargs["temperature"] = 1

        client_kwargs: dict[str, Any] = {}
        if session_id and retention != "none":
            client_kwargs["default_headers"] = {"x-session-affinity": session_id}
        client = anthropic.AsyncAnthropic(**client_kwargs)
        async for event in iter_anthropic_sdk_stream(
            client,
            stream_kwargs,
            provider="claude",
            error_message="Claude stream error: %s",
            n_messages=len(messages),
            n_tools=len(tools),
        ):
            yield event
