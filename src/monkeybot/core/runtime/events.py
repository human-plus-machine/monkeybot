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
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


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
    parse_error: str | None = None
    call_id: str = ""
    inspector_decision: str | None = None
    resource: str | None = None
    resolved_path: str | None = None


@dataclass(frozen=True)
class ToolCallResult:
    kind: Literal["ToolCallResult"] = "ToolCallResult"
    request_id: str = ""
    tool: str = ""
    result: str = ""
    error: str | None = None
    call_id: str = ""
    error_kind: str | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class TurnComplete:
    kind: Literal["TurnComplete"] = "TurnComplete"
    request_id: str = ""
    usage: UsageTotals = field(default_factory=UsageTotals)
    trace_id: str | None = None


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
    """Prompt-token estimate at summarization start (preflight for token pressure; counted on demand for count pressure)."""
    context_window_tokens: int = 0


@dataclass(frozen=True)
class ContextSummarized:
    kind: Literal["ContextSummarized"] = "ContextSummarized"
    request_id: str = ""
    turns_summarized: int = 0


@dataclass(frozen=True)
class ContextUsage:
    """Live current prompt size for context meters (not peak, not a summarization signal)."""

    kind: Literal["ContextUsage"] = "ContextUsage"
    request_id: str = ""
    estimated_tokens: int = 0
    """Current preflight / post-tool / post-compaction prompt size for UI meters."""
    context_window_tokens: int = 0
    inner_turn: int = 0


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
    image_id: str = ""
    mime_type: str = ""
    # Base64 pixels when no durable workspace path is available.
    data: str = ""
    # Workspace-relative path; preferred over data for UI clients.
    path: str = ""


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
    notification_type: Literal["thinkingMessage", "inlineMessage", "creditsExhausted"] = (
        "inlineMessage"
    )
    msg: str = ""
    data: dict[str, object] | None = None


@dataclass(frozen=True)
class ConfigReloaded:
    """Live-only: gateway applied a new ``RuntimeConfig`` revision (not a restart)."""

    kind: Literal["ConfigReloaded"] = "ConfigReloaded"
    request_id: str = ""
    revision: int = 0
    digest: str = ""
    hot: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    restart_required: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GroundingEvent:
    """Provider-native web-search grounding metadata (e.g. Gemini ``google_search``).

    Additive: distinct from the harness's ``web_search`` custom tool (``ToolCallStarted``/
    ``ToolCallResult``); this surfaces citations from a provider-hosted search invoked
    server-side during the model call.
    """

    kind: Literal["GroundingEvent"] = "GroundingEvent"
    request_id: str = ""
    sources: list[dict[str, str]] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AttachmentDescriptorEvent:
    """Frozen attachment metadata for playground UI (not persisted as a ContentBlock)."""

    kind: Literal["AttachmentDescriptor"] = "AttachmentDescriptor"
    request_id: str = ""
    attachment_id: str = ""
    mime_type: str = ""
    filename: str = ""
    description: str = ""


@dataclass(frozen=True)
class UserSteered:
    """User text injected mid-turn at a safe loop boundary (steer queue)."""

    kind: Literal["UserSteered"] = "UserSteered"
    request_id: str = ""
    text: str = ""


@dataclass(frozen=True)
class QueuedInputAccepted:
    """Steer or follow-up prompt accepted into a session admission queue."""

    kind: Literal["QueuedInputAccepted"] = "QueuedInputAccepted"
    request_id: str = ""
    queue: Literal["steer", "follow_up"] = "follow_up"
    position: int = 0


@dataclass(frozen=True)
class ContextEpochStarted:
    """New context epoch opened (session start, post-compaction, or stable-source change)."""

    kind: Literal["ContextEpochStarted"] = "ContextEpochStarted"
    request_id: str = ""
    epoch_id: int = 0
    changed_sources: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SystemContextUpdated:
    """Volatile system-context sources changed within the current epoch."""

    kind: Literal["SystemContextUpdated"] = "SystemContextUpdated"
    request_id: str = ""
    epoch_id: int = 0
    changed_sources: list[str] = field(default_factory=list)
    # Injected body for transcript NDJSON only. ``event_to_json`` omits it;
    # ``TranscriptWriter._debug_fields`` writes it back onto the file record.
    text: str = ""


@dataclass(frozen=True)
class AssistantTextStarted:
    """Start of an assistant text block (additive; ``AssistantDelta`` still streams)."""

    kind: Literal["AssistantTextStarted"] = "AssistantTextStarted"
    request_id: str = ""


@dataclass(frozen=True)
class AssistantTextEnded:
    """End of an assistant text block (additive).

    When ``text`` is set, this is a durable settlement boundary (full block text
    for replay). Streaming clients may still reconstruct from ``AssistantDelta``.
    """

    kind: Literal["AssistantTextEnded"] = "AssistantTextEnded"
    request_id: str = ""
    text: str = ""


@dataclass(frozen=True)
class ThinkingBlockStarted:
    """Start of a thinking/reasoning block (additive; deltas still use ThinkingBlockDelta)."""

    kind: Literal["ThinkingBlockStarted"] = "ThinkingBlockStarted"
    request_id: str = ""


@dataclass(frozen=True)
class ToolInputDeltaEvent:
    """Incremental tool-argument JSON while the model streams a tool call (additive).

    ``delta`` is a raw, opaque fragment of one streaming JSON document per
    ``call_id`` — not valid JSON by itself. Clients must buffer fragments by
    ``call_id`` (in arrival order) and only attempt to parse once the tool call
    is finalized (``ToolCallStarted``); never parse an individual ``delta``.
    """

    kind: Literal["ToolInputDelta"] = "ToolInputDelta"
    request_id: str = ""
    call_id: str = ""
    tool: str = ""
    delta: str = ""


@dataclass(frozen=True)
class SubagentStarted:
    """Lifecycle marker: nested subagent spawn beginning (parent SSE)."""

    kind: Literal["SubagentStarted"] = "SubagentStarted"
    request_id: str = ""  # parent request_id
    parent_call_id: str = ""
    run_id: str = ""
    child_thread_id: str = ""
    subagent_type: str | None = None
    task: str = ""
    label: str = ""


@dataclass(frozen=True)
class SubagentEvent:
    """Wrapper for one forwarded child AgentEvent on the parent SSE bus."""

    kind: Literal["SubagentEvent"] = "SubagentEvent"
    request_id: str = ""  # parent request_id
    parent_call_id: str = ""
    run_id: str = ""
    child_thread_id: str = ""
    subagent_type: str | None = None
    inner: AgentEvent = field(kw_only=True)


@dataclass(frozen=True)
class SubagentCompleted:
    """Lifecycle marker: nested subagent drain finished (success/error/timeout/cancel)."""

    kind: Literal["SubagentCompleted"] = "SubagentCompleted"
    request_id: str = ""  # parent request_id
    parent_call_id: str = ""
    run_id: str = ""
    child_thread_id: str = ""
    subagent_type: str | None = None
    ok: bool = True
    final_message: str = ""
    errors: list[str] = field(default_factory=list)
    tool_call_count: int = 0


AgentEvent: TypeAlias = (
    Thinking
    | AssistantDelta
    | ToolCallStarted
    | ToolCallResult
    | TurnComplete
    | Error
    | ContextSummarizing
    | ContextSummarized
    | ContextUsage
    | SystemPromptSnapshot
    | ImageBlock
    | ThinkingBlockDelta
    | ThinkingBlockComplete
    | RedactedThinkingBlock
    | ToolConfirmationRequestEvent
    | ActionRequiredEvent
    | FrontendToolRequestEvent
    | SystemNotificationEvent
    | ConfigReloaded
    | AttachmentDescriptorEvent
    | GroundingEvent
    | UserSteered
    | QueuedInputAccepted
    | ContextEpochStarted
    | SystemContextUpdated
    | AssistantTextStarted
    | AssistantTextEnded
    | ThinkingBlockStarted
    | ToolInputDeltaEvent
    | SubagentStarted
    | SubagentEvent
    | SubagentCompleted
)

# Durable vs live-only (OpenCode V2-style). Conversation history persists
# Message/ContentBlock rows separately; this set classifies AgentEvent kinds for
# transcript filtering and replay semantics.
#
# Durable = settlement / boundary events that should survive for debugging replay.
# Everything else is live-only (streaming fragments and ephemeral UI progress).
DURABLE_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "ToolCallStarted",  # tool input finalized (args known); history mirror = ToolRequest
        "ToolCallResult",  # tool success/failure settlement; history mirror = ToolResponse
        "TurnComplete",
        "Error",
        "ContextSummarized",
        "ThinkingBlockComplete",
        "RedactedThinkingBlock",
        "AssistantTextEnded",  # when text is set, full settled prose
        "ToolConfirmationRequest",
        "GroundingEvent",
        "UserSteered",
        "QueuedInputAccepted",
        "ContextEpochStarted",
        "SystemContextUpdated",
        "SubagentStarted",  # nested spawn boundary (parent SSE)
        "SubagentCompleted",  # nested drain boundary (parent SSE)
        # SubagentEvent is live-only; durable nested transcript is the child thread.
    }
)

# v1 allowlist of child AgentEvent kinds that may be forwarded as SubagentEvent.inner.
SUBAGENT_FORWARD_KINDS: frozenset[str] = frozenset(
    {
        "AssistantDelta",
        "AssistantTextStarted",
        "AssistantTextEnded",
        "ThinkingBlockStarted",
        "ThinkingBlockDelta",
        "ThinkingBlockComplete",
        "RedactedThinkingBlock",
        "ToolCallStarted",
        "ToolCallResult",
        "ToolInputDelta",
        "Error",
        "TurnComplete",
    }
)


def is_durable_event(event: AgentEvent) -> bool:
    """Return True when ``event`` is a durable settlement/boundary event."""
    return event.kind in DURABLE_EVENT_KINDS


def is_subagent_forwardable(event: AgentEvent) -> bool:
    """Return True when ``event.kind`` is in the v1 nested-forward allowlist (1B)."""
    return event.kind in SUBAGENT_FORWARD_KINDS


def wrap_subagent_event(
    *,
    request_id: str,
    parent_call_id: str,
    run_id: str,
    child_thread_id: str,
    subagent_type: str | None,
    inner: AgentEvent,
) -> SubagentEvent | None:
    """Wrap ``inner`` as ``SubagentEvent`` if allowlisted; otherwise return ``None``.

    Denylisted kinds (SystemPromptSnapshot, Context*, confirmations, images,
    grounding, steer/queue, nested Subagent*, legacy Thinking, etc.) are dropped
    by returning None — callers must not publish.
    """
    if not is_subagent_forwardable(inner):
        return None
    return SubagentEvent(
        request_id=request_id,
        parent_call_id=parent_call_id,
        run_id=run_id,
        child_thread_id=child_thread_id,
        subagent_type=subagent_type,
        inner=inner,
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
    crt = int(d.get("cache_read_tokens", 0))
    cct = int(d.get("cache_creation_tokens", 0))
    cost_raw = d.get("cost_usd", 0.0)
    cost = float(cost_raw) if isinstance(cost_raw, (int, float)) else 0.0
    dur = int(d.get("duration_ms", 0))
    ept = int(d.get("estimated_prompt_tokens", 0))
    return UsageTotals(
        input_tokens=it,
        output_tokens=ot,
        cached_tokens=ct,
        cache_read_tokens=crt,
        cache_creation_tokens=cct,
        cost_usd=cost,
        duration_ms=dur,
        estimated_prompt_tokens=ept,
    )


def _context_token_fields(payload: dict[str, Any]) -> tuple[int, int]:
    """Parse ``estimated_tokens`` / ``context_window_tokens`` from a wire payload."""
    et_raw = payload.get("estimated_tokens", 0)
    cwt_raw = payload.get("context_window_tokens", 0)
    et = int(et_raw) if isinstance(et_raw, (int, float)) else 0
    cwt = int(cwt_raw) if isinstance(cwt_raw, (int, float)) else 0
    return et, cwt


def _args_from_obj(raw: object | None) -> dict[str, object]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise EventDecodeError("ToolCallStarted args must be a JSON object")
    return dict(raw)


def _subagent_correlation_fields(
    event: SubagentStarted | SubagentEvent | SubagentCompleted,
) -> dict[str, object]:
    """Shared parent-bus correlation fields for nested Subagent* wire payloads."""
    return {
        "parent_call_id": event.parent_call_id,
        "run_id": event.run_id,
        "child_thread_id": event.child_thread_id,
        "subagent_type": event.subagent_type,
    }


def _parse_subagent_type(raw: object | None) -> str | None:
    """Parse optional ``subagent_type`` (null/missing → None; non-str → error)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    raise EventDecodeError("subagent_type must be a string or null")


def _parse_subagent_correlation(
    payload: dict[str, Any],
) -> tuple[str, str, str, str | None]:
    """Parse parent_call_id, run_id, child_thread_id, subagent_type from wire."""
    pc_raw = payload.get("parent_call_id", "")
    parent_call_id = pc_raw if isinstance(pc_raw, str) else ""
    run_raw = payload.get("run_id", "")
    run_id = run_raw if isinstance(run_raw, str) else ""
    ct_raw = payload.get("child_thread_id", "")
    child_thread_id = ct_raw if isinstance(ct_raw, str) else ""
    subagent_type = _parse_subagent_type(payload.get("subagent_type"))
    return parent_call_id, run_id, child_thread_id, subagent_type


def _story5_event_dict(event: AgentEvent) -> dict[str, object]:
    """Build JSON-serializable dict for Story 5 SSE types (1B §4.2; snake_case keys)."""
    base: dict[str, object] = {"type": event.kind, "request_id": event.request_id}
    if isinstance(event, ImageBlock):
        out: dict[str, object] = {**base, "mime_type": event.mime_type}
        if event.image_id:
            out["image_id"] = event.image_id
        # Prefer path so clients load from disk; omit megabyte base64 when possible.
        if event.path:
            out["path"] = event.path
        else:
            out["data"] = event.data
        return out
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
    if isinstance(event, ConfigReloaded):
        return {
            **base,
            "revision": event.revision,
            "digest": event.digest,
            "hot": list(event.hot),
            "applied": list(event.applied),
            "restart_required": list(event.restart_required),
        }
    if isinstance(event, AttachmentDescriptorEvent):
        return {
            **base,
            "attachment_id": event.attachment_id,
            "mime_type": event.mime_type,
            "filename": event.filename,
            "description": event.description,
        }
    if isinstance(event, GroundingEvent):
        return {
            **base,
            "sources": [dict(s) for s in event.sources],
            "search_queries": list(event.search_queries),
        }
    if isinstance(event, UserSteered):
        return {**base, "text": event.text}
    if isinstance(event, QueuedInputAccepted):
        return {**base, "queue": event.queue, "position": event.position}
    if isinstance(event, ContextEpochStarted):
        return {
            **base,
            "epoch_id": event.epoch_id,
            "changed_sources": list(event.changed_sources),
        }
    if isinstance(event, SystemContextUpdated):
        return {
            **base,
            "epoch_id": event.epoch_id,
            "changed_sources": list(event.changed_sources),
        }
    if isinstance(event, AssistantTextEnded):
        out = dict(base)
        if event.text:
            out["text"] = event.text
        return out
    if isinstance(event, (AssistantTextStarted, ThinkingBlockStarted)):
        return base
    if isinstance(event, ToolInputDeltaEvent):
        return {
            **base,
            "call_id": event.call_id,
            "tool": event.tool,
            "delta": event.delta,
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
        if event.parse_error is not None:
            payload["parse_error"] = event.parse_error
        if event.call_id:
            payload["call_id"] = event.call_id
    elif isinstance(event, ToolCallResult):
        payload = {**base, "tool": event.tool, "result": event.result, "error": event.error}
        if event.call_id:
            payload["call_id"] = event.call_id
    elif isinstance(event, TurnComplete):
        u = event.usage
        payload = {
            **base,
            "usage": {
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "cached_tokens": u.cached_tokens,
                "cache_read_tokens": u.cache_read_tokens,
                "cache_creation_tokens": u.cache_creation_tokens,
                "cost_usd": u.cost_usd,
                "duration_ms": u.duration_ms,
                "estimated_prompt_tokens": u.estimated_prompt_tokens,
            },
        }
        if event.trace_id:
            payload["trace_id"] = event.trace_id
    elif isinstance(event, Error):
        payload = {**base, "error": event.error}
    elif isinstance(event, (ContextSummarizing, ContextUsage)):
        payload = {
            **base,
            "estimated_tokens": event.estimated_tokens,
            "context_window_tokens": event.context_window_tokens,
        }
        if isinstance(event, ContextUsage) and event.inner_turn:
            payload["inner_turn"] = event.inner_turn
    elif isinstance(event, ContextSummarized):
        payload = {**base, "turns_summarized": event.turns_summarized}
    elif isinstance(event, SystemPromptSnapshot):
        payload = {**base, "inner_turn": event.inner_turn, "text": event.text}
    elif isinstance(event, SubagentStarted):
        payload = {
            **base,
            **_subagent_correlation_fields(event),
            "task": event.task,
            "label": event.label,
        }
    elif isinstance(event, SubagentCompleted):
        payload = {
            **base,
            **_subagent_correlation_fields(event),
            "ok": event.ok,
            "final_message": event.final_message,
            "errors": list(event.errors),
            "tool_call_count": event.tool_call_count,
        }
    elif isinstance(event, SubagentEvent):
        payload = {
            **base,
            **_subagent_correlation_fields(event),
            "inner": json.loads(event_to_json(event.inner)),
        }
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
            ConfigReloaded,
            AttachmentDescriptorEvent,
            GroundingEvent,
            UserSteered,
            QueuedInputAccepted,
            ContextEpochStarted,
            SystemContextUpdated,
            AssistantTextStarted,
            AssistantTextEnded,
            ThinkingBlockStarted,
            ToolInputDeltaEvent,
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
    return _event_from_dict(decoded)


def _event_from_dict(payload: dict[str, Any]) -> AgentEvent:
    """Decode a parsed JSON object into an AgentEvent (accepts ``type`` or ``kind``)."""
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
        pe_raw = payload.get("parse_error")
        parse_error: str | None = pe_raw if isinstance(pe_raw, str) else None
        call_id_raw = payload.get("call_id", "")
        call_id = call_id_raw if isinstance(call_id_raw, str) else ""
        return ToolCallStarted(
            request_id=rid,
            tool=tool,
            label=label,
            args=args,
            parse_error=parse_error,
            call_id=call_id,
            inspector_decision=(
                payload.get("inspector_decision")
                if isinstance(payload.get("inspector_decision"), str)
                else None
            ),
            resource=payload.get("resource") if isinstance(payload.get("resource"), str) else None,
            resolved_path=(
                payload.get("resolved_path")
                if isinstance(payload.get("resolved_path"), str)
                else None
            ),
        )
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
        call_id_raw = payload.get("call_id", "")
        call_id = call_id_raw if isinstance(call_id_raw, str) else ""
        kind_raw = payload.get("error_kind")
        error_kind = kind_raw if isinstance(kind_raw, str) else None
        dur_raw = payload.get("duration_ms")
        duration_ms = (
            int(dur_raw)
            if isinstance(dur_raw, (int, float)) and not isinstance(dur_raw, bool)
            else None
        )
        return ToolCallResult(
            request_id=rid,
            tool=tool,
            result=result,
            error=err,
            call_id=call_id,
            error_kind=error_kind,
            duration_ms=duration_ms,
        )
    if t == "TurnComplete":
        usage = _usage_from_obj(payload.get("usage"))
        trace_id_raw = payload.get("trace_id")
        trace_id: str | None
        if trace_id_raw is None:
            trace_id = None
        elif isinstance(trace_id_raw, str) and trace_id_raw:
            trace_id = trace_id_raw
        else:
            raise EventDecodeError("TurnComplete trace_id must be a non-empty string")
        return TurnComplete(request_id=rid, usage=usage, trace_id=trace_id)
    if t == "Error":
        err_raw = payload.get("error", "")
        err = err_raw if isinstance(err_raw, str) else ""
        return Error(request_id=rid, error=err)
    if t == "ContextSummarizing":
        et, cwt = _context_token_fields(payload)
        return ContextSummarizing(request_id=rid, estimated_tokens=et, context_window_tokens=cwt)
    if t == "ContextSummarized":
        ts_raw = payload.get("turns_summarized", 0)
        ts = int(ts_raw) if isinstance(ts_raw, (int, float)) else 0
        return ContextSummarized(request_id=rid, turns_summarized=ts)
    if t == "ContextUsage":
        et, cwt = _context_token_fields(payload)
        it_raw = payload.get("inner_turn", 0)
        inner_turn = int(it_raw) if isinstance(it_raw, (int, float)) else 0
        return ContextUsage(
            request_id=rid,
            estimated_tokens=et,
            context_window_tokens=cwt,
            inner_turn=inner_turn,
        )
    if t == "SystemPromptSnapshot":
        it_raw = payload.get("inner_turn", 0)
        inner_turn = int(it_raw) if isinstance(it_raw, (int, float)) else 0
        text_raw = payload.get("text", "")
        text = text_raw if isinstance(text_raw, str) else ""
        return SystemPromptSnapshot(request_id=rid, inner_turn=inner_turn, text=text)
    if t == "ImageBlock":
        mt = payload.get("mime_type", "")
        data = payload.get("data", "")
        img_raw = payload.get("image_id", "")
        path_raw = payload.get("path", "")
        mime_type = mt if isinstance(mt, str) else ""
        data_s = data if isinstance(data, str) else ""
        image_id = img_raw if isinstance(img_raw, str) else ""
        path = path_raw.strip() if isinstance(path_raw, str) else ""
        return ImageBlock(
            request_id=rid,
            image_id=image_id,
            mime_type=mime_type,
            data=data_s,
            path=path,
        )
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
        return ActionRequiredEvent(request_id=rid, action_type=at, id=el_id, payload=pl)
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
        return SystemNotificationEvent(request_id=rid, notification_type=nt, msg=msg, data=data_obj)
    if t == "ConfigReloaded":
        rev_raw = payload.get("revision", 0)
        revision = int(rev_raw) if isinstance(rev_raw, (int, float)) else 0
        digest_raw = payload.get("digest", "")
        digest = digest_raw if isinstance(digest_raw, str) else ""

        def _str_list(key: str) -> list[str]:
            raw = payload.get(key)
            if not isinstance(raw, list):
                return []
            return [item for item in raw if isinstance(item, str)]

        return ConfigReloaded(
            request_id=rid,
            revision=revision,
            digest=digest,
            hot=_str_list("hot"),
            applied=_str_list("applied"),
            restart_required=_str_list("restart_required"),
        )
    if t == "GroundingEvent":
        sources_raw = payload.get("sources")
        sources: list[dict[str, str]] = []
        if isinstance(sources_raw, list):
            for item in sources_raw:
                if isinstance(item, dict):
                    sources.append({str(k): str(v) for k, v in item.items()})
        queries_raw = payload.get("search_queries")
        queries = [str(q) for q in queries_raw] if isinstance(queries_raw, list) else []
        return GroundingEvent(request_id=rid, sources=sources, search_queries=queries)
    if t == "AttachmentDescriptor":
        aid = payload.get("attachment_id", "")
        mime = payload.get("mime_type", "")
        fname = payload.get("filename", "")
        desc = payload.get("description", "")
        return AttachmentDescriptorEvent(
            request_id=rid,
            attachment_id=aid if isinstance(aid, str) else "",
            mime_type=mime if isinstance(mime, str) else "",
            filename=fname if isinstance(fname, str) else "",
            description=desc if isinstance(desc, str) else "",
        )
    if t == "UserSteered":
        text_raw = payload.get("text", "")
        text = text_raw if isinstance(text_raw, str) else ""
        return UserSteered(request_id=rid, text=text)
    if t == "QueuedInputAccepted":
        q_raw = payload.get("queue", "follow_up")
        if q_raw not in ("steer", "follow_up"):
            raise EventDecodeError("QueuedInputAccepted queue must be steer or follow_up")
        q = cast(Literal["steer", "follow_up"], q_raw)
        pos_raw = payload.get("position", 0)
        pos = int(pos_raw) if isinstance(pos_raw, (int, float)) else 0
        return QueuedInputAccepted(request_id=rid, queue=q, position=pos)
    if t == "ContextEpochStarted":
        eid_raw = payload.get("epoch_id", 0)
        eid = int(eid_raw) if isinstance(eid_raw, (int, float)) else 0
        src_raw = payload.get("changed_sources")
        changed: list[str] = [str(s) for s in src_raw] if isinstance(src_raw, list) else []
        return ContextEpochStarted(request_id=rid, epoch_id=eid, changed_sources=changed)
    if t == "SystemContextUpdated":
        eid_raw = payload.get("epoch_id", 0)
        eid = int(eid_raw) if isinstance(eid_raw, (int, float)) else 0
        src_raw = payload.get("changed_sources")
        changed = [str(s) for s in src_raw] if isinstance(src_raw, list) else []
        text_raw = payload.get("text", "")
        text = text_raw if isinstance(text_raw, str) else ""
        return SystemContextUpdated(
            request_id=rid,
            epoch_id=eid,
            changed_sources=changed,
            text=text,
        )
    if t == "AssistantTextStarted":
        return AssistantTextStarted(request_id=rid)
    if t == "AssistantTextEnded":
        text_raw = payload.get("text", "")
        text = text_raw if isinstance(text_raw, str) else ""
        return AssistantTextEnded(request_id=rid, text=text)
    if t == "ThinkingBlockStarted":
        return ThinkingBlockStarted(request_id=rid)
    if t == "ToolInputDelta":
        cid_raw = payload.get("call_id", "")
        tool_raw = payload.get("tool", "")
        delta_raw = payload.get("delta", "")
        return ToolInputDeltaEvent(
            request_id=rid,
            call_id=cid_raw if isinstance(cid_raw, str) else "",
            tool=tool_raw if isinstance(tool_raw, str) else "",
            delta=delta_raw if isinstance(delta_raw, str) else "",
        )
    if t == "SubagentStarted":
        parent_call_id, run_id, child_thread_id, subagent_type = _parse_subagent_correlation(
            payload
        )
        task_raw = payload.get("task", "")
        label_raw = payload.get("label", "")
        return SubagentStarted(
            request_id=rid,
            parent_call_id=parent_call_id,
            run_id=run_id,
            child_thread_id=child_thread_id,
            subagent_type=subagent_type,
            task=task_raw if isinstance(task_raw, str) else "",
            label=label_raw if isinstance(label_raw, str) else "",
        )
    if t == "SubagentEvent":
        parent_call_id, run_id, child_thread_id, subagent_type = _parse_subagent_correlation(
            payload
        )
        inner_raw = payload.get("inner")
        if not isinstance(inner_raw, dict):
            raise EventDecodeError("SubagentEvent inner must be an object")
        inner_event = _event_from_dict(inner_raw)
        return SubagentEvent(
            request_id=rid,
            parent_call_id=parent_call_id,
            run_id=run_id,
            child_thread_id=child_thread_id,
            subagent_type=subagent_type,
            inner=inner_event,
        )
    if t == "SubagentCompleted":
        parent_call_id, run_id, child_thread_id, subagent_type = _parse_subagent_correlation(
            payload
        )
        ok_raw = payload.get("ok", True)
        if not isinstance(ok_raw, bool):
            raise EventDecodeError("SubagentCompleted ok must be a boolean")
        fm_raw = payload.get("final_message", "")
        final_message = fm_raw if isinstance(fm_raw, str) else ""
        errors_raw = payload.get("errors", [])
        if not isinstance(errors_raw, list):
            raise EventDecodeError("SubagentCompleted errors must be a list")
        errors = [str(e) for e in errors_raw]
        tcc_raw = payload.get("tool_call_count", 0)
        tool_call_count = int(tcc_raw) if isinstance(tcc_raw, (int, float)) else 0
        return SubagentCompleted(
            request_id=rid,
            parent_call_id=parent_call_id,
            run_id=run_id,
            child_thread_id=child_thread_id,
            subagent_type=subagent_type,
            ok=ok_raw,
            final_message=final_message,
            errors=errors,
            tool_call_count=tool_call_count,
        )
    raise EventDecodeError(f"unknown AgentEvent type: {t!r}")
