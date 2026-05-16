"""Test doubles for :class:`~monkeybot.core.llm.provider.Provider` without network."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from monkeybot.core.llm.provider import Message, ProviderEvent
from monkeybot.core.types.types_tools import ToolDef


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
    ) -> AsyncIterator[ProviderEvent]:
        # Copy protects against callers mutating the original list mid-stream.
        for ev in list(self._events):
            yield ev


__all__ = ["ScriptedFakeProvider"]
