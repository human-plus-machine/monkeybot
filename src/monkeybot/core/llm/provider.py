"""Thin provider streaming contract (LLM adapter boundary)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from monkeybot.core.types.content_blocks import ContentBlock, Text, ToolRequest, ToolResponse
from monkeybot.core.types.types_tools import ToolDef

Role: TypeAlias = Literal["user", "assistant", "system"]


@dataclass(frozen=True, kw_only=True)
class Message:
    """Single turn for tool-calling chat models (typed content blocks)."""

    role: Role
    content: list[ContentBlock]

    def __post_init__(self) -> None:
        if self.role not in ("user", "assistant", "system"):
            raise ValueError(f"invalid role: {self.role!r}")
        for i, block in enumerate(self.content):
            if not isinstance(block, ContentBlock):
                raise ValueError(
                    f"content[{i}] must be ContentBlock, got {type(block).__name__}"
                )

    def to_dict(self) -> dict[str, object]:
        return {"role": self.role, "content": [b.to_dict() for b in self.content]}

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> Message:
        role = d.get("role")
        if role not in ("user", "assistant", "system"):
            raise ValueError(f"invalid role: {role!r}")
        raw_blocks = d.get("content", [])
        if raw_blocks is None:
            raw_blocks = []
        if not isinstance(raw_blocks, list):
            raise ValueError("content must be a list")
        blocks: list[ContentBlock] = []
        for item in raw_blocks:
            if not isinstance(item, dict):
                raise ValueError("content list elements must be JSON objects")
            blocks.append(ContentBlock.from_dict(item))
        return cls(role=role, content=blocks)

    @classmethod
    def text(cls, role: Role, text: str) -> Message:
        """Build a message whose content is a single :class:`Text` block."""
        return cls(role=role, content=[Text(text=text)])


@dataclass(frozen=True, kw_only=True)
class TextDelta:
    kind: Literal["text_delta"] = "text_delta"
    text: str


@dataclass(frozen=True, kw_only=True)
class ThinkingDelta:
    """Incremental thinking/reasoning chunk from a streaming provider."""

    kind: Literal["thinking_delta"] = "thinking_delta"
    text: str
    signature: str | None = None


@dataclass(frozen=True, kw_only=True)
class ToolCall:
    kind: Literal["tool_call"] = "tool_call"
    call_id: str
    name: str
    args: dict[str, object]
    parse_error: str | None = None
    metadata: dict[str, object] | None = None


@dataclass(frozen=True, kw_only=True)
class UsageEvent:
    kind: Literal["usage"] = "usage"
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass(frozen=True)
class Done:
    kind: Literal["done"] = "done"


ProviderEvent: TypeAlias = TextDelta | ThinkingDelta | ToolCall | UsageEvent | Done


class Provider(Protocol):
    """Streams model output as :class:`ProviderEvent` values.

    Exactly one consumer should iterate a given ``stream`` at a time; concurrent
    overlapping calls on the same instance are intentionally undefined.

    ``stream`` is annotated as a synchronous method returning :class:`AsyncIterator`
    so async-generator implementations match under strict mypy (``async def``
    with ``yield`` is *not* typed as returning ``Coroutine[..., AsyncIterator]``).
    """

    @property
    def name(self) -> str:
        """Stable provider id (e.g. ``\"gemini\"``)."""

    @property
    def supports_streaming(self) -> bool:
        """Whether partial output is exposed as incremental deltas."""

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Yield provider events for one model request.

        ``thinking_budget`` overrides the configured reasoning budget for this call
        when the provider supports it (Gemini, Claude). ``None`` uses the default.
        """

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> int:
        """Return the provider-aligned input (prompt) token count for one outbound request.

        Must reflect the same payload shape as :meth:`stream` (messages, tools, model),
        typically via the vendor's tokenizer or count API — not post-hoc usage from a
        prior response.
        ``thinking_budget`` mirrors :meth:`stream` for providers whose token count
        changes with reasoning configuration.
        """


__all__ = [
    "Done",
    "Message",
    "Provider",
    "ProviderEvent",
    "Role",
    "Text",
    "TextDelta",
    "ThinkingDelta",
    "ToolCall",
    "ToolDef",
    "ToolRequest",
    "ToolResponse",
    "UsageEvent",
]
