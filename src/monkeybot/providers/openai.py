"""OpenAI Chat Completions provider (direct ``openai`` SDK, async streaming)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.llm.provider import (
    Message,
    ProviderCallHints,
    ProviderEvent,
)
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._openai_compat import (
    count_openai_compat_input_tokens,
    iter_openai_compat_stream,
    messages_to_openai,
    openai_messages_token_count,
    openai_tools,
    openai_tools_token_count,
)
from monkeybot.providers.sampling import resolve_model_sampling

# Re-export private names that existing tests import directly from this module.
_messages_to_openai = messages_to_openai
_openai_tools = openai_tools
_openai_messages_token_count = openai_messages_token_count
_openai_tools_token_count = openai_tools_token_count

__all__ = [
    "OpenAIProvider",
    # legacy private names kept for tests
    "_messages_to_openai",
    "_openai_tools",
    "_openai_messages_token_count",
    "_openai_tools_token_count",
]


class OpenAIProvider:
    """OpenAI chat models using the official ``openai`` async client."""

    @property
    def name(self) -> str:
        return "openai"

    @property
    def supports_streaming(self) -> bool:
        return True

    def __init__(
        self,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is not set")
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
        import tiktoken  # noqa: PLC0415

        msgs = list(messages)
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        return count_openai_compat_input_tokens(enc, msgs, tools)

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
        from openai import AsyncOpenAI  # noqa: PLC0415

        msgs = list(messages)

        system, oai_messages = messages_to_openai(msgs)
        if system:
            oai_messages = [{"role": "system", "content": system}, *oai_messages]

        retention = hints.cache_retention if hints is not None else "short"
        session_id = hints.session_id if hints is not None else None
        client_kwargs: dict[str, Any] = {}
        if session_id and retention != "none":
            client_kwargs["default_headers"] = {"x-session-affinity": session_id}
        client = AsyncOpenAI(**client_kwargs)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "stream": True,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if tools:
            kwargs["tools"] = openai_tools(tools)
            kwargs["parallel_" + "tool" + "_calls"] = True
        if retention == "long":
            kwargs["prompt_cache_retention"] = "24h"

        async for event in iter_openai_compat_stream(
            client,
            kwargs,
            provider="openai",
            n_messages=len(messages),
            n_tools=len(tools),
        ):
            yield event
