"""YAML script events mapped onto provider events.

The vocabulary is the §5.2 ``script`` row: ``text``, ``thinking``, ``tool_call``,
``tool_input_delta``, ``grounding``, ``usage``, ``done``, and ``raise``.
``raise`` is a script control, not a :class:`~monkeybot.core.llm.provider.ProviderEvent`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, TypeAlias, assert_never

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from monkeybot.core.llm.provider import Done as ProviderDone
from monkeybot.core.llm.provider import GroundingEvent as ProviderGrounding
from monkeybot.core.llm.provider import ProviderEvent, TextDelta, ThinkingDelta
from monkeybot.core.llm.provider import ToolCall as ProviderToolCall
from monkeybot.core.llm.provider import ToolInputDelta as ProviderToolInputDelta
from monkeybot.core.llm.provider import UsageEvent as ProviderUsage


class _YamlEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextEvent(_YamlEvent):
    type: Literal["text"]
    text: str


class ThinkingEvent(_YamlEvent):
    type: Literal["thinking"]
    text: str
    signature: str | None = None


class ToolCallEvent(_YamlEvent):
    type: Literal["tool_call"]
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    parse_error: str | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("args", mode="before")
    @classmethod
    def _empty_args(cls, value: object) -> object:
        if value is None:
            return {}
        return value


class ToolInputDeltaEvent(_YamlEvent):
    type: Literal["tool_input_delta"]
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    delta: str


class GroundingEvent(_YamlEvent):
    type: Literal["grounding"]
    sources: list[dict[str, str]] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)


class UsageEvent(_YamlEvent):
    type: Literal["usage"]
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_creation_tokens: int = Field(default=0, ge=0)


class DoneEvent(_YamlEvent):
    type: Literal["done"]
    truncated: bool = False


class RaiseEvent(_YamlEvent):
    """Stop the scripted call. ``error`` is an exception class name.

    ``after`` is how many events of this call to yield before raising. When it
    is omitted, the event's position in the call is the trigger. Nothing here
    raises; the provider seam does.
    """

    type: Literal["raise"]
    error: str = Field(min_length=1)
    after: int | None = Field(default=None, ge=0)
    message: str | None = None


_ScriptEvent: TypeAlias = (
    TextEvent
    | ThinkingEvent
    | ToolCallEvent
    | ToolInputDeltaEvent
    | GroundingEvent
    | UsageEvent
    | DoneEvent
    | RaiseEvent
)
ScriptEvent: TypeAlias = Annotated[_ScriptEvent, Field(discriminator="type")]

_EVENT_ADAPTER: TypeAdapter[ScriptEvent] = TypeAdapter(ScriptEvent)


def parse_event(data: Mapping[str, Any]) -> ScriptEvent:
    """Parse one YAML event object."""
    return _EVENT_ADAPTER.validate_python(data)


def _object_dict(value: Mapping[str, Any]) -> dict[str, object]:
    copied: dict[str, object] = {}
    copied.update(value)
    return copied


def to_provider_event(event: ScriptEvent) -> ProviderEvent:
    """Map a YAML event onto the provider dataclass of the same kind."""
    if isinstance(event, TextEvent):
        return TextDelta(text=event.text)
    if isinstance(event, ThinkingEvent):
        return ThinkingDelta(text=event.text, signature=event.signature)
    if isinstance(event, ToolCallEvent):
        metadata = None if event.metadata is None else _object_dict(event.metadata)
        return ProviderToolCall(
            call_id=event.call_id,
            name=event.name,
            args=_object_dict(event.args),
            parse_error=event.parse_error,
            metadata=metadata,
        )
    if isinstance(event, ToolInputDeltaEvent):
        return ProviderToolInputDelta(call_id=event.call_id, name=event.name, delta=event.delta)
    if isinstance(event, GroundingEvent):
        return ProviderGrounding(
            sources=[dict(source) for source in event.sources],
            search_queries=list(event.search_queries),
        )
    if isinstance(event, UsageEvent):
        return ProviderUsage(
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            cached_tokens=event.cached_tokens,
            cache_read_tokens=event.cache_read_tokens,
            cache_creation_tokens=event.cache_creation_tokens,
        )
    if isinstance(event, DoneEvent):
        return ProviderDone(truncated=event.truncated)
    if isinstance(event, RaiseEvent):
        raise TypeError("raise is not a ProviderEvent")
    assert_never(event)


__all__ = [
    "DoneEvent",
    "GroundingEvent",
    "RaiseEvent",
    "ScriptEvent",
    "TextEvent",
    "ThinkingEvent",
    "ToolCallEvent",
    "ToolInputDeltaEvent",
    "UsageEvent",
    "parse_event",
    "to_provider_event",
]
