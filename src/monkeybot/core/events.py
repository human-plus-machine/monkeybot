"""Typed agent events with NDJSON-compatible serialization.

Wire format uses discriminator ``type`` per SSE payloads; Python dataclasses use
field ``kind`` internally. Serialization maps ``kind`` ↔ ``type``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from monkeybot.core.interfaces import MonkeybotError


class EventDecodeError(MonkeybotError):  # type: ignore[misc]
    """Invalid or unsupported AgentEvent JSON.

    Incoming payloads may expose the discriminator under ``\"type\"`` (canonical
    SSE field) or ``\"kind\"`` as an alias.
    """


@dataclass(frozen=True)
class UsageTotals:
    """Token and cost rollup attached to TurnComplete."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0


@dataclass(frozen=True)
class Thinking:
    kind: Literal["Thinking"] = "Thinking"
    request_id: str = ""


@dataclass(frozen=True)
class AssistantDelta:
    kind: Literal["AssistantDelta"] = "AssistantDelta"
    request_id: str = ""
    delta: str = ""


@dataclass(frozen=True)
class ToolCallStarted:
    kind: Literal["ToolCallStarted"] = "ToolCallStarted"
    request_id: str = ""
    tool: str = ""
    label: str = ""
    args: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallResult:
    kind: Literal["ToolCallResult"] = "ToolCallResult"
    request_id: str = ""
    tool: str = ""
    result: str = ""
    error: str | None = None


@dataclass(frozen=True)
class TurnComplete:
    kind: Literal["TurnComplete"] = "TurnComplete"
    request_id: str = ""
    usage: UsageTotals = field(default_factory=UsageTotals)


@dataclass(frozen=True)
class Error:
    kind: Literal["Error"] = "Error"
    request_id: str = ""
    error: str = ""


@dataclass(frozen=True)
class ContextSummarizing:
    kind: Literal["ContextSummarizing"] = "ContextSummarizing"
    request_id: str = ""
    estimated_tokens: int = 0
    context_window_tokens: int = 0


@dataclass(frozen=True)
class ContextSummarized:
    kind: Literal["ContextSummarized"] = "ContextSummarized"
    request_id: str = ""
    turns_summarized: int = 0


AgentEvent: TypeAlias = (
    Thinking
    | AssistantDelta
    | ToolCallStarted
    | ToolCallResult
    | TurnComplete
    | Error
    | ContextSummarizing
    | ContextSummarized
)


def _usage_from_obj(raw: object | None) -> UsageTotals:
    if raw is None:
        return UsageTotals()
    if not isinstance(raw, dict):
        return UsageTotals()
    d = raw
    it = int(d.get("input_tokens", 0))
    ot = int(d.get("output_tokens", 0))
    ct = int(d.get("cached_tokens", 0))
    cost_raw = d.get("cost_usd", 0.0)
    cost = float(cost_raw) if isinstance(cost_raw, (int, float)) else 0.0
    dur = int(d.get("duration_ms", 0))
    return UsageTotals(
        input_tokens=it,
        output_tokens=ot,
        cached_tokens=ct,
        cost_usd=cost,
        duration_ms=dur,
    )


def _args_from_obj(raw: object | None) -> dict[str, object]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise EventDecodeError("ToolCallStarted args must be a JSON object")
    return dict(raw)


def event_to_json(event: AgentEvent) -> str:
    """Serialize ``event`` to a compact JSON line with ``type`` discriminator."""
    base: dict[str, object] = {"type": event.kind, "request_id": event.request_id}
    if isinstance(event, Thinking):
        payload = base
    elif isinstance(event, AssistantDelta):
        payload = {**base, "delta": event.delta}
    elif isinstance(event, ToolCallStarted):
        payload = {**base, "tool": event.tool, "label": event.label, "args": dict(event.args)}
    elif isinstance(event, ToolCallResult):
        payload = {**base, "tool": event.tool, "result": event.result, "error": event.error}
    elif isinstance(event, TurnComplete):
        u = event.usage
        payload = {
            **base,
            "usage": {
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "cached_tokens": u.cached_tokens,
                "cost_usd": u.cost_usd,
                "duration_ms": u.duration_ms,
            },
        }
    elif isinstance(event, Error):
        payload = {**base, "error": event.error}
    elif isinstance(event, ContextSummarizing):
        payload = {
            **base,
            "estimated_tokens": event.estimated_tokens,
            "context_window_tokens": event.context_window_tokens,
        }
    elif isinstance(event, ContextSummarized):
        payload = {**base, "turns_summarized": event.turns_summarized}
    else:
        raise EventDecodeError(f"Unsupported AgentEvent variant: {type(event).__name__}")
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise EventDecodeError("Failed to serialize event to JSON") from exc


def event_from_json(raw: str) -> AgentEvent:
    """Parse ``raw`` JSON into an AgentEvent (accepts ``type`` or alias ``kind``)."""
    try:
        decoded: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EventDecodeError("invalid JSON in AgentEvent payload") from exc
    if not isinstance(decoded, dict):
        raise EventDecodeError("AgentEvent payload must be a JSON object")
    payload = decoded
    t = payload.get("type") or payload.get("kind")
    if t is None:
        raise EventDecodeError("missing type in AgentEvent JSON")
    if not isinstance(t, str):
        raise EventDecodeError("event type discriminator must be a string")
    request_id_raw = payload.get("request_id", "")
    rid = request_id_raw if isinstance(request_id_raw, str) else ""

    if t == "Thinking":
        return Thinking(request_id=rid)
    if t == "AssistantDelta":
        delta_raw = payload.get("delta", "")
        delta = delta_raw if isinstance(delta_raw, str) else ""
        return AssistantDelta(request_id=rid, delta=delta)
    if t == "ToolCallStarted":
        tool_raw = payload.get("tool", "")
        label_raw = payload.get("label", "")
        tool = tool_raw if isinstance(tool_raw, str) else ""
        label = label_raw if isinstance(label_raw, str) else ""
        args = _args_from_obj(payload.get("args"))
        return ToolCallStarted(request_id=rid, tool=tool, label=label, args=args)
    if t == "ToolCallResult":
        tool_raw = payload.get("tool", "")
        result_raw = payload.get("result", "")
        tool = tool_raw if isinstance(tool_raw, str) else ""
        result = result_raw if isinstance(result_raw, str) else ""
        err_raw = payload.get("error")
        err: str | None
        if err_raw is None:
            err = None
        elif isinstance(err_raw, str):
            err = err_raw
        else:
            raise EventDecodeError("ToolCallResult error must be a string or null")
        return ToolCallResult(request_id=rid, tool=tool, result=result, error=err)
    if t == "TurnComplete":
        usage = _usage_from_obj(payload.get("usage"))
        return TurnComplete(request_id=rid, usage=usage)
    if t == "Error":
        err_raw = payload.get("error", "")
        err = err_raw if isinstance(err_raw, str) else ""
        return Error(request_id=rid, error=err)
    if t == "ContextSummarizing":
        et_raw = payload.get("estimated_tokens", 0)
        cwt_raw = payload.get("context_window_tokens", 0)
        et = int(et_raw) if isinstance(et_raw, (int, float)) else 0
        cwt = int(cwt_raw) if isinstance(cwt_raw, (int, float)) else 0
        return ContextSummarizing(
            request_id=rid, estimated_tokens=et, context_window_tokens=cwt
        )
    if t == "ContextSummarized":
        ts_raw = payload.get("turns_summarized", 0)
        ts = int(ts_raw) if isinstance(ts_raw, (int, float)) else 0
        return ContextSummarized(request_id=rid, turns_summarized=ts)
    raise EventDecodeError(f"unknown AgentEvent type: {t!r}")
