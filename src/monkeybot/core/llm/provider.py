"""Thin provider streaming contract (LLM adapter boundary)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias, cast

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
class ToolInputDelta:
    """Incremental tool-argument JSON fragment while args are still streaming.

    ``delta`` is a raw, opaque fragment of a single streaming JSON document keyed
    by ``call_id`` (e.g. Anthropic ``input_json_delta``). It is not valid JSON on
    its own — consumers must concatenate all fragments for a given ``call_id``
    (in arrival order) before attempting to parse the result. The final,
    validated arguments are delivered separately on :class:`ToolCall`.
    """

    kind: Literal["tool_input_delta"] = "tool_input_delta"
    call_id: str
    name: str
    delta: str


@dataclass(frozen=True, kw_only=True)
class GroundingEvent:
    """Provider-native web-search grounding metadata (e.g. Gemini ``google_search``).

    Additive to the harness's pluggable ``web_search`` custom tool — this carries
    citations/search-suggestion data from a provider-hosted search tool invoked
    server-side, not a tool call the harness dispatched itself.
    """

    kind: Literal["grounding"] = "grounding"
    sources: list[dict[str, str]]
    search_queries: list[str]


@dataclass(frozen=True, kw_only=True)
class ProviderCallHints:
    """Optional transport metadata for prompt-cache / session affinity.

    Content strategy (stable/volatile split + epoch) lives in prompts; these
    hints are provider-specific request options layered on top.

    ``cache_retention``:
    - Anthropic-family: ``short`` = 5m ephemeral, ``long`` = 1h ``ttl``,
      ``none`` = no ``cache_control`` markers.
    - OpenAI: ``long`` sets ``prompt_cache_retention=24h``; ``short``/``none``
      leave the API default.
    """

    session_id: str | None = None
    cache_retention: Literal["none", "short", "long"] = "short"


def cache_retention_from_env() -> Literal["none", "short", "long"]:
    """Resolve ``MODEL_CACHE_RETENTION`` (default ``short``)."""
    from monkeybot.core.config.snapshot import current_env

    raw = current_env("MODEL_CACHE_RETENTION", "short").strip().lower()
    if raw in ("none", "short", "long"):
        return raw  # type: ignore[return-value]
    return "short"


def gemini_extra_kwargs(provider: Provider, *, vertex_google_search: bool) -> dict[str, bool]:
    if vertex_google_search and provider.name == "gemini":
        return {"vertex_google_search": True}
    return {}


def provider_call_hints_kwargs(
    provider: Provider,
    hints: ProviderCallHints | None,
) -> dict[str, Any]:
    """Return kwargs accepted by providers that opt into ``ProviderCallHints``."""
    if hints is None:
        return {}
    # Anthropic-family + OpenAI accept hints; others ignore via Protocol default.
    if provider.name in ("claude", "vertex-claude", "bedrock", "openai"):
        return {"hints": hints}
    return {}


async def provider_count_input_tokens(
    provider: Provider,
    messages: Sequence[Message],
    tools: Sequence[ToolDef],
    *,
    model: str,
    thinking_budget: int | None = None,
    vertex_google_search: bool = False,
    hints: ProviderCallHints | None = None,
) -> int:
    kwargs: dict[str, Any] = {"model": model, "thinking_budget": thinking_budget}
    kwargs.update(gemini_extra_kwargs(provider, vertex_google_search=vertex_google_search))
    kwargs.update(provider_call_hints_kwargs(provider, hints))
    if kwargs.keys() - {"model", "thinking_budget"}:
        return int(
            await cast(Any, provider).count_input_tokens(messages, tools, **kwargs)
        )
    return await provider.count_input_tokens(messages, tools, **kwargs)


def provider_stream(
    provider: Provider,
    messages: Sequence[Message],
    tools: Sequence[ToolDef],
    *,
    model: str,
    thinking_budget: int | None = None,
    vertex_google_search: bool = False,
    hints: ProviderCallHints | None = None,
) -> AsyncIterator[ProviderEvent]:
    kwargs: dict[str, Any] = {"model": model, "thinking_budget": thinking_budget}
    kwargs.update(gemini_extra_kwargs(provider, vertex_google_search=vertex_google_search))
    kwargs.update(provider_call_hints_kwargs(provider, hints))
    if kwargs.keys() - {"model", "thinking_budget"}:
        return cast(
            AsyncIterator[ProviderEvent],
            cast(Any, provider).stream(messages, tools, **kwargs),
        )
    return provider.stream(messages, tools, **kwargs)


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
    """End of a provider stream. ``truncated`` means the vendor hit an output length limit."""

    kind: Literal["done"] = "done"
    truncated: bool = False


ProviderEvent: TypeAlias = (
    TextDelta
    | ThinkingDelta
    | ToolCall
    | ToolInputDelta
    | GroundingEvent
    | UsageEvent
    | Done
)


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
    "GroundingEvent",
    "Message",
    "Provider",
    "ProviderCallHints",
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
    "cache_retention_from_env",
    "gemini_extra_kwargs",
    "provider_call_hints_kwargs",
    "provider_count_input_tokens",
    "provider_stream",
]
