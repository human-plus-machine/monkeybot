"""OpenAI Chat Completions provider (direct ``openai`` SDK, async streaming)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.llm.provider import (
    Done,
    Message,
    ProviderEvent,
    TextDelta,
    ToolCall,
    UsageEvent,
)
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._openai_compat import (
    iter_openai_compat_stream,
    messages_to_openai,
    openai_messages_token_count,
    openai_tools,
    openai_tools_token_count,
)

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

# Keep these names importable to avoid breaking the unused-import linter
_ = (Done, TextDelta, ToolCall, UsageEvent)  # consumed via _openai_compat re-export


class OpenAIProvider:
    """OpenAI chat models using the official ``openai`` async client."""

    @property
    def name(self) -> str:
        return "openai"

    @property
    def supports_streaming(self) -> bool:
        return True

    def __init__(self) -> None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is not set")

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
    ) -> int:
        import tiktoken  # noqa: PLC0415

        msgs = list(messages)
        system, oai_messages = messages_to_openai(msgs)
        if system:
            oai_messages = [{"role": "system", "content": system}, *oai_messages]
        tool_defs = openai_tools(tools) if tools else []
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        return openai_messages_token_count(enc, oai_messages) + openai_tools_token_count(enc, tool_defs)

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
    ) -> AsyncIterator[ProviderEvent]:
        from openai import AsyncOpenAI  # noqa: PLC0415

        msgs = list(messages)
        temperature = float(os.environ.get("MODEL_TEMPERATURE", "0.7"))
        max_tokens = int(os.environ.get("MODEL_MAX_TOKENS", "4096"))

        system, oai_messages = messages_to_openai(msgs)
        if system:
            oai_messages = [{"role": "system", "content": system}, *oai_messages]

        client = AsyncOpenAI()
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = openai_tools(tools)
            kwargs["parallel_" + "tool" + "_calls"] = True

        async for event in iter_openai_compat_stream(client, kwargs):
            yield event
