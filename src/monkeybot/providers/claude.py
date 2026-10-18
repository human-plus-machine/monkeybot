"""Anthropic Claude provider (direct API, async streaming)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from monkeybot.core.llm.provider import Message, ProviderCallHints, ProviderEvent
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._utils import (
    anthropic_sampling_kwargs,
    anthropic_tool_defs,
    build_anthropic_messages,
    count_anthropic_input_tokens,
    iter_anthropic_sdk_stream,
    prepare_anthropic_cached_payload,
    split_leading_system,
    suppress_thinking_for_auxiliary_call,
)
from monkeybot.providers.request_budget import trim_message_media_for_byte_budget
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
        reasoning_effort: str | None = None,
    ) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY is not set")
        sampling = resolve_model_sampling(
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        self._temperature = sampling.temperature
        self._max_tokens = sampling.max_tokens
        self._reasoning_effort = sampling.reasoning_effort

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

        messages = trim_message_media_for_byte_budget(
            messages,
            tools,
            provider=self.name,
            raise_if_oversized=False,
        )
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

        messages = trim_message_media_for_byte_budget(messages, tools, provider=self.name)
        system, msgs = split_leading_system(messages)
        system_param, converted_messages, tools_param = prepare_anthropic_cached_payload(
            system=system,
            messages=msgs,
            tools=tools,
            cache_retention=retention,
            not_given=anthropic.NOT_GIVEN,
        )

        auxiliary = suppress_thinking_for_auxiliary_call(messages, tools)
        stream_kwargs: dict[str, Any] = {
            "model": model,
            "system": system_param,
            "messages": cast(Any, converted_messages),
            "tools": tools_param,
            "max_tokens": self._max_tokens,
            **anthropic_sampling_kwargs(
                model=model,
                temperature=self._temperature,
                thinking_budget=None if auxiliary else thinking_budget,
                reasoning_effort=None if auxiliary else self._reasoning_effort,
                provider="claude",
            ),
        }

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
            fallback_temperature=self._temperature,
        ):
            yield event
