"""Test doubles for :class:`~monkeybot.core.llm.provider.Provider` without network."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence

from monkeybot.core.llm.provider import Message, ProviderEvent
from monkeybot.core.types.content_blocks import ContentBlock, Text, ToolRequest, ToolResponse
from monkeybot.core.types.types_tools import ToolDef


def fake_provider_prompt_tokens(messages: Sequence[Message], tools: Sequence[ToolDef]) -> int:
    """Deterministic char÷4 tally for fake providers in unit tests (no vendor APIs)."""

    def char_blocks(blocks: Sequence[ContentBlock]) -> int:
        total = 0
        for b in blocks:
            if isinstance(b, Text):
                total += len(b.text)
            elif isinstance(b, ToolRequest):
                total += len(b.id) + len(b.name) + len(json.dumps(b.args, sort_keys=True))
            elif isinstance(b, ToolResponse):
                total += len(b.id) + len(b.tool_name) + char_blocks(b.result)
            else:
                total += len(json.dumps(b.to_dict(), sort_keys=True))
        return total

    n = sum(char_blocks(m.content) for m in messages) // 4
    for t in tools:
        n += (
            len(t.name)
            + len(t.description)
            + len(json.dumps(t.input_schema, sort_keys=True, default=str))
        ) // 4
    return max(0, n)


class ScriptedFakeProvider:
    """Deterministic provider yielding a fixed :class:`~monkeybot.core.llm.provider.ProviderEvent` list."""

    def __init__(
        self,
        events: list[ProviderEvent],
        *,
        name: str = "fake",
        supports_streaming: bool = True,
    ) -> None:
        self._events = list(events)
        self._name = name
        self._supports_streaming = supports_streaming

    @property
    def name(self) -> str:
        return self._name

    @property
    def supports_streaming(self) -> bool:
        return self._supports_streaming

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del thinking_budget
        # Copy protects against callers mutating the original list mid-stream.
        for ev in list(self._events):
            yield ev

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
    ) -> int:
        del model
        return fake_provider_prompt_tokens(messages, tools)


__all__ = ["ScriptedFakeProvider", "fake_provider_prompt_tokens"]
