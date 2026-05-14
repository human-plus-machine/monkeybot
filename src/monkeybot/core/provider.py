"""Thin provider streaming contract (LLM adapter boundary)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from monkeybot.core.types_tools import ToolDef


@dataclass(frozen=True)
class Message:
    """Single turn message for tool-calling capable chat models."""

    role: Literal["user", "assistant", "system", "tool"]
    content: str
    tool_name: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class TextDelta:
    kind: Literal["text_delta"] = "text_delta"
    text: str


@dataclass(frozen=True, kw_only=True)
class ToolCall:
    kind: Literal["tool_call"] = "tool_call"
    call_id: str
    name: str
    args: dict[str, object]


@dataclass(frozen=True, kw_only=True)
class UsageEvent:
    kind: Literal["usage"] = "usage"
    input_tokens: int
    output_tokens: int
    cached_tokens: int


@dataclass(frozen=True)
class Done:
    kind: Literal["done"] = "done"


ProviderEvent: TypeAlias = TextDelta | ToolCall | UsageEvent | Done


class Provider(Protocol):
    """Streams model output as :class:`ProviderEvent` values.

    Exactly one consumer should iterate a given ``stream`` at a time; concurrent
    overlapping calls on the same instance are intentionally undefined.
    """

    @property
    def name(self) -> str:
        """Stable provider id (e.g. ``\"gemini\"``)."""

    @property
    def supports_streaming(self) -> bool:
        """Whether partial output is exposed as incremental deltas."""

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
    ) -> AsyncIterator[ProviderEvent]:
        """Yield provider events for one model request."""


__all__ = [
    "Done",
    "Message",
    "Provider",
    "ProviderEvent",
    "TextDelta",
    "ToolCall",
    "UsageEvent",
]
