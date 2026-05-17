"""Typed agent events with NDJSON-compatible serialization.

Wire format uses discriminator ``type`` per SSE payloads; Python dataclasses use
field ``kind`` internally. Serialization maps ``kind`` ↔ ``type``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias, cast

from monkeybot.core.types.interfaces import MonkeybotError


class EventDecodeError(MonkeybotError):
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
    estimated_prompt_tokens: int = 0


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
    """Pre-stream input token count that tripped the summarization threshold (same basis as ``estimated_prompt_tokens`` on usage)."""
    context_window_tokens: int = 0


@dataclass(frozen=True)
class ContextSummarized:
    kind: Literal["ContextSummarized"] = "ContextSummarized"
    request_id: str = ""
    turns_summarized: int = 0


@dataclass(frozen=True)
class SystemPromptSnapshot:
    """Full system message text sent to the provider on this inner-loop iteration (playground / debug)."""

    kind: Literal["SystemPromptSnapshot"] = "SystemPromptSnapshot"
    request_id: str = ""
    inner_turn: int = 0
    """1-based inner loop index for this user message (increments on tool follow-ups)."""
    text: str = ""


@dataclass(frozen=True)
class ImageBlock:
    kind: Literal["ImageBlock"] = "ImageBlock"
    request_id: str = ""
    mime_type: str = ""
    data: str = ""


@dataclass(frozen=True)
class ThinkingBlockDelta:
    kind: Literal["ThinkingBlockDelta"] = "ThinkingBlockDelta"
    request_id: str = ""
    text: str = ""
    signature: str | None = None


@dataclass(frozen=True)
class ThinkingBlockComplete:
    kind: Literal["ThinkingBlockComplete"] = "ThinkingBlockComplete"
    request_id: str = ""
    signature: str = ""


@dataclass(frozen=True)
class RedactedThinkingBlock:
    kind: Literal["RedactedThinkingBlock"] = "RedactedThinkingBlock"
    request_id: str = ""
    data: str = ""


@dataclass(frozen=True)
class ToolConfirmationRequestEvent:
    kind: Literal["ToolConfirmationRequest"] = "ToolConfirmationRequest"
    request_id: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    arguments: dict[str, object] = field(default_factory=dict)
    prompt: str | None = None


@dataclass(frozen=True)
class ActionRequiredEvent:
    kind: Literal["ActionRequiredEvent"] = "ActionRequiredEvent"
    request_id: str = ""
    action_type: Literal["elicitation", "toolConfirmation", "elicitationResponse"] = "elicitation"
    id: str = ""
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class FrontendToolRequestEvent:
    kind: Literal["FrontendToolRequest"] = "FrontendToolRequest"
    request_id: str = ""
    tool_call_id: str = ""
    name: str = ""
    args: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SystemNotificationEvent:
    kind: Literal["SystemNotificationEvent"] = "SystemNotificationEvent"
    request_id: str = ""
    notification_type: Literal["thinkingMessage", "inlineMessage", "creditsExhausted"] = "inlineMessage"
    msg: str = ""
    data: dict[str, object] | None = None


AgentEvent: TypeAlias = (
    Thinking
    | AssistantDelta
    | ToolCallStarted
    | ToolCallResult
    | TurnComplete
    | Error
    | ContextSummarizing
    | ContextSummarized
    | SystemPromptSnapshot
    | ImageBlock
    | ThinkingBlockDelta
    | ThinkingBlockComplete
    | RedactedThinkingBlock
    | ToolConfirmationRequestEvent
    | ActionRequiredEvent
    | FrontendToolRequestEvent
    | SystemNotificationEvent
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
    ept = int(d.get("estimated_prompt_tokens", 0))
    return UsageTotals(
        input_tokens=it,
        output_tokens=ot,
        cached_tokens=ct,
        cost_usd=cost,
        duration_ms=dur,
        estimated_prompt_tokens=ept,
    )


def _args_from_obj(raw: object | None) -> dict[str, object]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise EventDecodeError("ToolCallStarted args must be a JSON object")
    return dict(raw)


def _story5_event_dict(event: AgentEvent) -> dict[str, object]:
    """Build JSON-serializable dict for Story 5 SSE types (1B §4.2; snake_case keys)."""
    base: dict[str, object] = {"type": event.kind, "request_id": event.request_id}
    if isinstance(event, ImageBlock):
        return {**base, "mime_type": event.mime_type, "data": event.data}
    if isinstance(event, ThinkingBlockDelta):
        return {**base, "text": event.text, "signature": event.signature}
    if isinstance(event, ThinkingBlockComplete):
        return {**base, "signature": event.signature}
    if isinstance(event, RedactedThinkingBlock):
        return {**base, "data": event.data}
    if isinstance(event, ToolConfirmationRequestEvent):
        return {
            **base,
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "arguments": dict(event.arguments),
            "prompt": event.prompt,
        }
    if isinstance(event, ActionRequiredEvent):
        return {
            **base,
            "action_type": event.action_type,
            "id": event.id,
            "payload": dict(event.payload),
        }
    if isinstance(event, FrontendToolRequestEvent):
        return {
            **base,
            "tool_call_id": event.tool_call_id,
            "name": event.name,
            "args": dict(event.args),
        }
    if isinstance(event, SystemNotificationEvent):
        return {
            **base,
            "notification_type": event.notification_type,
            "msg": event.msg,
            "data": event.data,
        }
    raise AssertionError(f"_story5_event_dict: unsupported type {type(event)!r}")


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
                "estimated_prompt_tokens": u.estimated_prompt_tokens,
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
    elif isinstance(event, SystemPromptSnapshot):
        payload = {**base, "inner_turn": event.inner_turn, "text": event.text}
    elif isinstance(
        event,
        (
            ImageBlock,
            ThinkingBlockDelta,
            ThinkingBlockComplete,
            RedactedThinkingBlock,
            ToolConfirmationRequestEvent,
            ActionRequiredEvent,
            FrontendToolRequestEvent,
            SystemNotificationEvent,
        ),
    ):
        payload = _story5_event_dict(event)
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
    if t == "SystemPromptSnapshot":
        it_raw = payload.get("inner_turn", 0)
        inner_turn = int(it_raw) if isinstance(it_raw, (int, float)) else 0
        text_raw = payload.get("text", "")
        text = text_raw if isinstance(text_raw, str) else ""
        return SystemPromptSnapshot(request_id=rid, inner_turn=inner_turn, text=text)
    if t == "ImageBlock":
        mt = payload.get("mime_type", "")
        data = payload.get("data", "")
        mime_type = mt if isinstance(mt, str) else ""
        data_s = data if isinstance(data, str) else ""
        return ImageBlock(request_id=rid, mime_type=mime_type, data=data_s)
    if t == "ThinkingBlockDelta":
        text_raw = payload.get("text", "")
        text = text_raw if isinstance(text_raw, str) else ""
        sig_raw = payload.get("signature")
        signature: str | None
        if sig_raw is None:
            signature = None
        elif isinstance(sig_raw, str):
            signature = sig_raw
        else:
            raise EventDecodeError("ThinkingBlockDelta signature must be a string or null")
        return ThinkingBlockDelta(request_id=rid, text=text, signature=signature)
    if t == "ThinkingBlockComplete":
        sig_raw = payload.get("signature", "")
        sig = sig_raw if isinstance(sig_raw, str) else ""
        return ThinkingBlockComplete(request_id=rid, signature=sig)
    if t == "RedactedThinkingBlock":
        data_raw = payload.get("data", "")
        data_r = data_raw if isinstance(data_raw, str) else ""
        return RedactedThinkingBlock(request_id=rid, data=data_r)
    if t == "ToolConfirmationRequest":
        tc = payload.get("tool_call_id", "")
        tn = payload.get("tool_name", "")
        args_raw = payload.get("arguments")
        if args_raw is None:
            arguments: dict[str, object] = {}
        elif isinstance(args_raw, dict):
            arguments = dict(args_raw)
        else:
            raise EventDecodeError("ToolConfirmationRequest arguments must be an object")
        tool_call_id = tc if isinstance(tc, str) else ""
        tool_name = tn if isinstance(tn, str) else ""
        pr = payload.get("prompt")
        prompt: str | None
        if pr is None:
            prompt = None
        elif isinstance(pr, str):
            prompt = pr
        else:
            raise EventDecodeError("ToolConfirmationRequest prompt must be a string or null")
        return ToolConfirmationRequestEvent(
            request_id=rid,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            prompt=prompt,
        )
    if t == "ActionRequiredEvent":
        at_raw = payload.get("action_type", "elicitation")
        if at_raw not in ("elicitation", "toolConfirmation", "elicitationResponse"):
            raise EventDecodeError("ActionRequiredEvent action_type invalid")
        at = cast(Literal["elicitation", "toolConfirmation", "elicitationResponse"], at_raw)
        id_raw = payload.get("id", "")
        el_id = id_raw if isinstance(id_raw, str) else ""
        pl_raw = payload.get("payload")
        if pl_raw is None:
            pl: dict[str, object] = {}
        elif isinstance(pl_raw, dict):
            pl = dict(pl_raw)
        else:
            raise EventDecodeError("ActionRequiredEvent payload must be an object")
        return ActionRequiredEvent(
            request_id=rid, action_type=at, id=el_id, payload=pl
        )
    if t == "FrontendToolRequest":
        tc = payload.get("tool_call_id", "")
        name_raw = payload.get("name", "")
        args_raw = payload.get("args")
        if args_raw is None:
            args_d: dict[str, object] = {}
        elif isinstance(args_raw, dict):
            args_d = dict(args_raw)
        else:
            raise EventDecodeError("FrontendToolRequest args must be an object")
        tool_call_id = tc if isinstance(tc, str) else ""
        name = name_raw if isinstance(name_raw, str) else ""
        return FrontendToolRequestEvent(
            request_id=rid, tool_call_id=tool_call_id, name=name, args=args_d
        )
    if t == "SystemNotificationEvent":
        nt_raw = payload.get("notification_type", "inlineMessage")
        if nt_raw not in ("thinkingMessage", "inlineMessage", "creditsExhausted"):
            raise EventDecodeError("SystemNotificationEvent notification_type invalid")
        nt = cast(Literal["thinkingMessage", "inlineMessage", "creditsExhausted"], nt_raw)
        msg_raw = payload.get("msg", "")
        msg = msg_raw if isinstance(msg_raw, str) else ""
        data_raw = payload.get("data")
        data_obj: dict[str, object] | None
        if data_raw is None:
            data_obj = None
        elif isinstance(data_raw, dict):
            data_obj = dict(data_raw)
        else:
            raise EventDecodeError("SystemNotificationEvent data must be an object or null")
        return SystemNotificationEvent(
            request_id=rid, notification_type=nt, msg=msg, data=data_obj
        )
    raise EventDecodeError(f"unknown AgentEvent type: {t!r}")
