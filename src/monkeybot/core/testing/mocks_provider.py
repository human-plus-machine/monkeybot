"""Test doubles for :class:`~monkeybot.core.llm.provider.Provider` without network."""

from __future__ import annotations

import builtins
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import cast

import yaml

from monkeybot.core.llm.provider import (
    Done,
    GroundingEvent,
    Message,
    ProviderCallHints,
    ProviderEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolInputDelta,
    UsageEvent,
)
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
        thinking_budget: int | None = None,
    ) -> int:
        del model, thinking_budget
        return fake_provider_prompt_tokens(messages, tools)


_SCRIPT_ENV = "MONKEYBOT_FAKE_PROVIDER_SCRIPT"

_PROVIDER_EVENT_TYPES = (
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolInputDelta,
    GroundingEvent,
    UsageEvent,
    Done,
)

# Order is the error text tests and authors see.
_EVENT_TYPES: tuple[str, ...] = (
    "text",
    "thinking",
    "tool_call",
    "tool_input_delta",
    "grounding",
    "usage",
    "done",
    "raise",
)

_EVENT_FIELDS: dict[str, frozenset[str]] = {
    "text": frozenset({"text"}),
    "thinking": frozenset({"text", "signature"}),
    "tool_call": frozenset({"call_id", "name", "args", "parse_error", "metadata"}),
    "tool_input_delta": frozenset({"call_id", "name", "delta"}),
    "grounding": frozenset({"sources", "search_queries"}),
    "usage": frozenset(
        {
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        }
    ),
    "done": frozenset({"truncated"}),
    "raise": frozenset({"error", "message", "after"}),
}


class ScriptedProviderExhaustedError(RuntimeError):
    """``stream()`` was called more times than the script has turns."""


@dataclass(frozen=True, kw_only=True)
class RaiseStep:
    """Raise ``error`` from a scripted turn, optionally after N provider events.

    ``after is None`` raises when this step is reached: events listed before it
    are yielded, and events listed after it are a script error. ``after`` is a
    count of provider events to yield from the turn (including events listed
    after this step) before raising. Extra events beyond that count are not
    yielded.
    """

    error: type[BaseException]
    message: str = ""
    after: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.error, type) or not issubclass(self.error, BaseException):
            raise TypeError("RaiseStep.error must be an exception class")
        if self.after is not None and (isinstance(self.after, bool) or self.after < 0):
            raise ValueError("RaiseStep.after must be an integer >= 0")


@dataclass(frozen=True, kw_only=True)
class CountInputTokensCall:
    """One recorded ``count_input_tokens`` invocation."""

    messages: tuple[Message, ...]
    tools: tuple[ToolDef, ...]
    model: str
    thinking_budget: int | None
    hints: ProviderCallHints | None


@dataclass(frozen=True, kw_only=True)
class _TurnPlan:
    events: tuple[ProviderEvent, ...]
    error: type[BaseException] | None = None
    message: str = ""


def _compile_turn(turn: object, *, index: int) -> _TurnPlan:
    label = f"ScriptedProvider turn {index + 1}"
    if isinstance(turn, (str, bytes)) or not isinstance(turn, Sequence):
        raise TypeError(f"{label} must be a list of events, got {type(turn).__name__}")
    raise_step: RaiseStep | None = None
    raise_at: int | None = None
    events: list[ProviderEvent] = []
    for item in turn:
        if isinstance(item, RaiseStep):
            if raise_step is not None:
                raise ValueError(f"{label} has more than one raise step")
            raise_step = item
            raise_at = len(events) if item.after is None else item.after
            continue
        if not isinstance(item, _PROVIDER_EVENT_TYPES):
            raise TypeError(
                f"{label} has {type(item).__name__}, expected a provider event or RaiseStep"
            )
        if raise_step is not None and raise_step.after is None:
            raise ValueError(
                f"{label} has events after a raise step; set after to the number of "
                "events to yield, or move those events before the raise"
            )
        events.append(item)
    if raise_step is None:
        return _TurnPlan(events=tuple(events))
    if raise_at is None or raise_at > len(events):
        raise ValueError(f"{label} raise after={raise_at} but the turn has {len(events)} event(s)")
    return _TurnPlan(
        events=tuple(events[:raise_at]),
        error=raise_step.error,
        message=raise_step.message,
    )


class ScriptedProvider:
    """Multi-turn provider: one event list per ``stream()`` call, strict by default.

    A call past the last turn raises :class:`ScriptedProviderExhaustedError` before
    yielding. That is not an empty completion. :class:`ScriptedFakeProvider`
    is a different double: one flat list, repeated on every call.
    """

    def __init__(
        self,
        turns: Sequence[Sequence[ProviderEvent | RaiseStep]],
        *,
        name: str = "fake",
        supports_streaming: bool = True,
    ) -> None:
        if isinstance(turns, (str, bytes)):
            raise TypeError("ScriptedProvider turns must be a list of event lists")
        self._turns = tuple(_compile_turn(turn, index=i) for i, turn in enumerate(turns))
        self._cursor = 0
        self._name = name
        self._supports_streaming = supports_streaming
        self.stream_calls = 0
        self.stream_messages: list[list[Message]] = []
        self.stream_tools: list[list[ToolDef]] = []
        self.stream_models: list[str] = []
        self.stream_hints: list[ProviderCallHints | None] = []
        self.count_input_tokens_calls: list[CountInputTokensCall] = []

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
        hints: ProviderCallHints | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del thinking_budget
        self.stream_calls += 1
        self.stream_messages.append(list(messages))
        self.stream_tools.append(list(tools))
        self.stream_models.append(model)
        self.stream_hints.append(hints)
        if self._cursor >= len(self._turns):
            raise ScriptedProviderExhaustedError(
                f"ScriptedProvider strict mode: stream() call {self.stream_calls} "
                f"is past the end of the script ({len(self._turns)} turn(s))"
            )
        plan = self._turns[self._cursor]
        self._cursor += 1
        for ev in plan.events:
            yield ev
        if plan.error is not None:
            raise plan.error(plan.message)

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        hints: ProviderCallHints | None = None,
    ) -> int:
        self.count_input_tokens_calls.append(
            CountInputTokensCall(
                messages=tuple(messages),
                tools=tuple(tools),
                model=model,
                thinking_budget=thinking_budget,
                hints=hints,
            )
        )
        return fake_provider_prompt_tokens(messages, tools)


def _as_str(value: object, *, where: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a string")
    return value


def _as_int(value: object, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where} must be an integer")
    return value


def _as_bool(value: object, *, where: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{where} must be a boolean")
    return value


def _require(mapping: Mapping[str, object], key: str, *, where: str) -> object:
    if key not in mapping:
        raise ValueError(f"{where} is missing {key!r}")
    return mapping[key]


def _string_dict(value: object, *, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    out: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{where} has a non-string key")
        out[key] = cast(object, item)
    return out


def _string_list(value: object, *, where: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list")
    return [_as_str(item, where=f"{where}[{i}]") for i, item in enumerate(value)]


def _sources(value: object, *, where: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list")
    sources: list[dict[str, str]] = []
    for i, item in enumerate(value):
        raw = _string_dict(item, where=f"{where}[{i}]")
        sources.append({key: _as_str(val, where=f"{where}[{i}].{key}") for key, val in raw.items()})
    return sources


def _optional_str(mapping: Mapping[str, object], key: str, *, where: str) -> str | None:
    if key not in mapping or mapping[key] is None:
        return None
    return _as_str(mapping[key], where=f"{where}.{key}")


def _exception_class(name: str, *, where: str) -> type[BaseException]:
    parts = name.split(".")
    if not name or not parts or any(not part.isidentifier() for part in parts):
        raise ValueError(
            f"{where}: {name!r} is not an exception class "
            "(use a builtin name or a dotted module.Class path)"
        )
    if len(parts) == 1:
        found = cast(object, getattr(builtins, parts[0], None))
    else:
        module_name = ".".join(parts[:-1])
        try:
            module = import_module(module_name)
        except ImportError as exc:
            raise ValueError(f"{where}: cannot import exception class {name!r}") from exc
        found = cast(object, getattr(module, parts[-1], None))
    if isinstance(found, type) and issubclass(found, BaseException):
        return found
    raise ValueError(
        f"{where}: {name!r} is not an exception class "
        "(use a builtin name or a dotted module.Class path)"
    )


def _event_from_mapping(raw: Mapping[str, object], *, where: str) -> ProviderEvent | RaiseStep:
    kind = raw.get("type")
    known = ", ".join(_EVENT_TYPES)
    if not isinstance(kind, str) or kind not in _EVENT_FIELDS:
        raise ValueError(f"{where}: event type {kind!r} is not one of {known}")
    unknown = sorted(set(raw) - _EVENT_FIELDS[kind] - {"type"})
    if unknown:
        names = ", ".join(unknown)
        raise ValueError(f"{where}: unknown keys for {kind}: {names}")
    if kind == "text":
        return TextDelta(text=_as_str(_require(raw, "text", where=where), where=f"{where}.text"))
    if kind == "thinking":
        return ThinkingDelta(
            text=_as_str(_require(raw, "text", where=where), where=f"{where}.text"),
            signature=_optional_str(raw, "signature", where=where),
        )
    if kind == "tool_call":
        args = _string_dict(raw["args"], where=f"{where}.args") if "args" in raw else {}
        metadata = (
            _string_dict(raw["metadata"], where=f"{where}.metadata")
            if "metadata" in raw and raw["metadata"] is not None
            else None
        )
        return ToolCall(
            call_id=_as_str(_require(raw, "call_id", where=where), where=f"{where}.call_id"),
            name=_as_str(_require(raw, "name", where=where), where=f"{where}.name"),
            args=args,
            parse_error=_optional_str(raw, "parse_error", where=where),
            metadata=metadata,
        )
    if kind == "tool_input_delta":
        return ToolInputDelta(
            call_id=_as_str(_require(raw, "call_id", where=where), where=f"{where}.call_id"),
            name=_as_str(_require(raw, "name", where=where), where=f"{where}.name"),
            delta=_as_str(_require(raw, "delta", where=where), where=f"{where}.delta"),
        )
    if kind == "grounding":
        return GroundingEvent(
            sources=_sources(raw.get("sources", []), where=f"{where}.sources"),
            search_queries=_string_list(
                raw.get("search_queries", []), where=f"{where}.search_queries"
            ),
        )
    if kind == "usage":
        return UsageEvent(
            input_tokens=_as_int(
                _require(raw, "input_tokens", where=where), where=f"{where}.input_tokens"
            ),
            output_tokens=_as_int(
                _require(raw, "output_tokens", where=where), where=f"{where}.output_tokens"
            ),
            cached_tokens=_as_int(raw.get("cached_tokens", 0), where=f"{where}.cached_tokens"),
            cache_read_tokens=_as_int(
                raw.get("cache_read_tokens", 0), where=f"{where}.cache_read_tokens"
            ),
            cache_creation_tokens=_as_int(
                raw.get("cache_creation_tokens", 0), where=f"{where}.cache_creation_tokens"
            ),
        )
    if kind == "done":
        return Done(truncated=_as_bool(raw.get("truncated", False), where=f"{where}.truncated"))
    after: int | None
    if "after" not in raw or raw["after"] is None:
        after = None
    else:
        after = _as_int(raw["after"], where=f"{where}.after")
    message_raw = raw.get("message", "")
    message = "" if message_raw is None else _as_str(message_raw, where=f"{where}.message")
    return RaiseStep(
        error=_exception_class(
            _as_str(_require(raw, "error", where=where), where=f"{where}.error"),
            where=f"{where}.error",
        ),
        message=message,
        after=after,
    )


def _mapping(value: object, *, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a mapping")
    return _string_dict(value, where=where)


def _load_json(text: str, *, where: str) -> object:
    try:
        return cast(object, json.loads(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{where}: invalid JSON ({exc.msg})") from exc


def _load_yaml(text: str, *, where: str) -> object:
    try:
        return cast(object, yaml.safe_load(text))
    except yaml.YAMLError as exc:
        raise ValueError(f"{where}: invalid YAML ({exc})") from exc


def _load_document(text: str, *, suffix: str, where: str) -> object:
    if suffix == ".json":
        return _load_json(text, where=where)
    if suffix in {".yaml", ".yml"}:
        return _load_yaml(text, where=where)
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return _load_json(text, where=where)
        except ValueError:
            return _load_yaml(text, where=where)
    return _load_yaml(text, where=where)


def _turns_from_document(data: object, *, where: str) -> list[list[ProviderEvent | RaiseStep]]:
    body = data
    if isinstance(data, dict):
        if set(data) != {"script"}:
            raise ValueError(
                f"{where}: a script file must be a list of turns, "
                "or a mapping with a single 'script' key"
            )
        body = cast(object, data["script"])
    if not isinstance(body, list):
        raise ValueError(f"{where}: script must be a list of turns")
    turns: list[list[ProviderEvent | RaiseStep]] = []
    for i, turn in enumerate(body):
        turn_where = f"{where}: turn {i + 1}"
        if not isinstance(turn, list):
            raise ValueError(f"{turn_where} must be a list of events, got {type(turn).__name__}")
        events: list[ProviderEvent | RaiseStep] = []
        for j, item in enumerate(turn):
            events.append(
                _event_from_mapping(
                    _mapping(item, where=f"{turn_where} event {j + 1}"),
                    where=f"{turn_where} event {j + 1}",
                )
            )
        turns.append(events)
    return turns


def load_scripted_provider(
    path: str | Path,
    *,
    name: str = "fake",
    supports_streaming: bool = True,
) -> ScriptedProvider:
    """Load a YAML or JSON script file into a strict :class:`ScriptedProvider`.

    The file is a list of turns, and each turn is a list of events. A mapping
    with a single ``script`` key is also accepted. Event ``type`` is one of
    ``text``, ``thinking``, ``tool_call``, ``tool_input_delta``, ``grounding``,
    ``usage``, ``done`` (optional ``truncated``), or ``raise`` (``error`` class
    name, optional ``message`` and ``after``).
    """
    file = Path(path).expanduser()
    where = str(file)
    if not file.is_file():
        raise FileNotFoundError(f"{_SCRIPT_ENV} file not found: {file}")
    data = _load_document(file.read_text(encoding="utf-8"), suffix=file.suffix.lower(), where=where)
    return ScriptedProvider(
        _turns_from_document(data, where=where),
        name=name,
        supports_streaming=supports_streaming,
    )


def scripted_provider_from_env(
    *,
    name: str = "fake",
    supports_streaming: bool = True,
) -> ScriptedProvider | None:
    """Build a provider from ``MONKEYBOT_FAKE_PROVIDER_SCRIPT``, or ``None`` if unset.

    The variable is not an ``ENV_MAP`` key, so a live config snapshot does not
    carry it. A snapshot value wins when one is present (including empty, which
    means unset). Otherwise the process environment is used.
    """
    from monkeybot.core.config.snapshot import current_env_or_none

    pinned = current_env_or_none(_SCRIPT_ENV)
    path = pinned.strip() if pinned is not None else os.environ.get(_SCRIPT_ENV, "").strip()
    if not path:
        return None
    return load_scripted_provider(path, name=name, supports_streaming=supports_streaming)


__all__ = [
    "CountInputTokensCall",
    "RaiseStep",
    "ScriptedFakeProvider",
    "ScriptedProvider",
    "ScriptedProviderExhaustedError",
    "fake_provider_prompt_tokens",
    "load_scripted_provider",
    "scripted_provider_from_env",
]
