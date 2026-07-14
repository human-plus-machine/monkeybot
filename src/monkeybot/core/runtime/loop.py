"""Owned agent loop: provider streaming, inspectors, tool dispatch, event emission."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import shutil
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from monkeybot.core.attachments.catalog import SessionAttachmentCatalog
from monkeybot.core.attachments.freeze import freeze_attachments_in_history
from monkeybot.core.attachments.store import AttachmentStore
from monkeybot.core.context import (
    MCP_REGISTRY_MUTATING_TOOLS,
    TurnContext,
    refresh_memory_index,
    refresh_tools_after_mcp_change,
)
from monkeybot.core.context.epoch import ContextEpochTracker, EpochAdmit, fingerprint_text
from monkeybot.core.context.memory_prompt import (
    MemoryPromptSelection,
    memory_index_fingerprint,
    prepare_memory_for_prompt,
)
from monkeybot.core.context.tool_output_policy import resolve_tool_budget
from monkeybot.core.context.tool_result_ingress import summarize_tool_result_text
from monkeybot.core.context.tool_shapers import (
    exceeds_tool_output_budget,
    shape_tool_text,
)
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import (
    Done,
    Message,
    Provider,
    ProviderCallHints,
    TextDelta,
    ToolCall,
    UsageEvent,
    cache_retention_from_env,
    provider_count_input_tokens,
    provider_stream,
)
from monkeybot.core.llm.usage import Usage
from monkeybot.core.logging_utils import kv
from monkeybot.core.messages import convert_to_provider, transform_context
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.persistence.transcript import TranscriptWriter
from monkeybot.core.prompts.prompt import (
    RUNTIME_NOTES_HEADING,
    compose_stable_baseline,
    compose_volatile_tail,
    compose_volatile_tail_parts,
    latest_user_message_text,
)
from monkeybot.core.runtime.context_budget import (
    ContextBudgeter,
    compute_context_pressure_tier,
    summarization_trigger_ratio_from_env,
)
from monkeybot.core.runtime.provider_stream_mapper import ProviderStreamMapper
from monkeybot.core.tools.inspector import InspectorToolCall, ToolInspector
from monkeybot.core.tools.permission import remember_always_approval, resource_for_call
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.content_blocks import (
    ContentBlock,
    File,
    Image,
    Text,
    ToolRequest,
    ToolResponse,
)
from monkeybot.core.types.content_blocks import Thinking as ThinkingBlock
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers.pricing import estimate_cost

from .events import (
    AgentEvent,
    AttachmentDescriptorEvent,
    ContextEpochStarted,
    ContextSummarized,
    ContextSummarizing,
    Error,
    ImageBlock,
    SystemContextUpdated,
    SystemPromptSnapshot,
    Thinking,
    ToolCallResult,
    ToolCallStarted,
    ToolConfirmationRequestEvent,
    TurnComplete,
    UsageTotals,
    UserSteered,
)
from .input_admission import InputAdmission, preview_text

logger = logging.getLogger("monkeybot.core.runtime.loop")


async def _await_history_write(task: asyncio.Task[None] | None) -> None:
    """Await a backgrounded history write, logging (never raising) on failure.

    The final assistant message is persisted off the token-streaming path, but it
    MUST land before any later ``history.load``/``reset`` (e.g. attachment freeze)
    so the row is not lost. Callers await this at the turn tail.
    """
    if task is None:
        return
    try:
        await task
    except Exception:
        logger.exception("background assistant history write failed")


def _effective_max_turns(max_turns: int | None) -> int:
    if max_turns is not None:
        return max_turns
    return int(os.getenv("MAX_TURNS", "50"))


def _effective_doom_loop_threshold() -> int:
    """Consecutive identical tool calls before doom-loop recovery.

    Default 3. Set ``DOOM_LOOP_THRESHOLD=0`` to disable. Applies whether the
    calls succeed or fail — identical name+args with no progress is the signal.
    """
    raw = os.getenv("DOOM_LOOP_THRESHOLD")
    if raw is None or raw.strip() == "":
        return 3
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "invalid DOOM_LOOP_THRESHOLD %s; using default 3",
            kv(value=raw),
        )
        return 3


def _tool_call_fingerprint(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True, default=str)}"


def _doom_loop_texts(tool_name: str, threshold: int) -> tuple[str, str]:
    """Return ``(error_event_text, recovery_system_note)``."""
    error = (
        f"Doom loop detected: tool {tool_name!r} called identically "
        f"{threshold} times"
    )
    note = (
        f"[Harness] {error}. Do not repeat the same call. Explain the situation "
        "to the user and take a different approach (different tool or args)."
    )
    return error, note


def _should_reject_tool_batch(
    calls: Sequence[ToolCall],
    *,
    truncated: bool,
) -> bool:
    """True when no tool in this provider turn is safe to execute.

    A length/max-tokens stop means every tool call may carry silently
    incomplete args. Also reject when every call already has ``parse_error``
    (incomplete JSON).
    """
    if not calls:
        return False
    if truncated:
        return True
    return all(c.parse_error for c in calls)


def _rejected_tool_batch_error(
    call: ToolCall,
    *,
    truncated: bool,
) -> str:
    if truncated:
        return (
            f'Tool call "{call.name}" was not executed: the response hit the '
            "output token limit, so its arguments may be truncated. Re-issue "
            "the tool call with complete arguments."
        )
    return call.parse_error or (
        f'Tool call "{call.name}" was not executed: incomplete tool arguments '
        "in this batch. Re-issue with complete arguments."
    )


def _doom_loop_exempt_names(tools: Sequence[ToolDef]) -> frozenset[str]:
    """Tool names marked ``doom_loop_exempt`` (identical-args polling is expected)."""
    return frozenset(t.name for t in tools if t.doom_loop_exempt)


@dataclasses.dataclass
class _DoomLoopTracker:
    """Tracks consecutive identical tool calls within one user message.

    Fingerprint is tool name + args. Outcome (ok vs error) does not reset the
    streak — successful no-progress loops (e.g. repeated screenshots) must trip
    the same guard as repeated failures.

    Tools in ``exempt_names`` (from ``ToolDef.doom_loop_exempt``) are ignored so
    legitimate polling (e.g. ``loop_status``) does not force a recovery turn.

    After a recovery turn is consumed, the tracker re-arms so a later streak in
    the same user message can trigger again. While ``triggered`` is set (between
    detection and ``consume_recovery``), further ``record`` calls are ignored so
    the same streak cannot re-fire mid-batch.
    """

    threshold: int
    exempt_names: frozenset[str] = dataclasses.field(default_factory=frozenset)
    streak_fp: str | None = None
    streak_count: int = 0
    triggered: bool = False
    force_no_tools: bool = False
    recovery_note: str | None = None
    triggered_tool: str | None = None
    _pending_error: str | None = None

    def record(self, name: str, args: dict[str, Any]) -> None:
        if self.threshold <= 0 or self.triggered or name in self.exempt_names:
            return
        fp = _tool_call_fingerprint(name, args)
        if fp == self.streak_fp:
            self.streak_count += 1
        else:
            self.streak_fp = fp
            self.streak_count = 1
        if self.streak_count < self.threshold:
            return
        self.triggered = True
        self.triggered_tool = name
        error, note = _doom_loop_texts(name, self.threshold)
        self._pending_error = error
        self.force_no_tools = True
        self.recovery_note = note

    def take_error(self) -> str | None:
        msg = self._pending_error
        self._pending_error = None
        return msg

    def consume_recovery(self) -> tuple[bool, str | None]:
        force = self.force_no_tools
        note = self.recovery_note
        self.force_no_tools = False
        self.recovery_note = None
        if force:
            # Re-arm so a second identical streak later in this user message
            # can trigger another recovery turn.
            self.triggered = False
            self.streak_fp = None
            self.streak_count = 0
            self.triggered_tool = None
        return force, note


def _usage_to_totals(u: Usage) -> UsageTotals:
    return UsageTotals(
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cached_tokens=u.cached_tokens,
        cache_read_tokens=u.cache_read_tokens,
        cache_creation_tokens=u.cache_creation_tokens,
        cost_usd=u.cost_usd,
        duration_ms=u.duration_ms,
        estimated_prompt_tokens=u.estimated_prompt_tokens,
    )


def _merge_usage_event(usage: Usage, ev: UsageEvent) -> None:
    usage.input_tokens += ev.input_tokens
    usage.output_tokens += ev.output_tokens
    usage.cached_tokens += ev.cached_tokens
    usage.cache_read_tokens += ev.cache_read_tokens
    usage.cache_creation_tokens += ev.cache_creation_tokens


def _normalize_user_content(user_content: str | list[ContentBlock]) -> list[ContentBlock]:
    if isinstance(user_content, str):
        return [Text(text=user_content)]
    return list(user_content)


def _user_text_from_content(blocks: Sequence[ContentBlock]) -> str:
    return " ".join(
        b.text.strip() for b in blocks if isinstance(b, Text) and b.text.strip()
    )


def _blocks_to_sse_summary(blocks: Sequence[ContentBlock]) -> str:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, Text):
            parts.append(block.text)
        elif isinstance(block, Image):
            meta = block.metadata or {}
            att_id = meta.get("attachment_id", "")
            parts.append(f"[loaded image {att_id}]" if att_id else "[loaded image]")
        elif isinstance(block, File):
            parts.append("[loaded pdf]")
    return "\n".join(parts)


def _system_message_from_text(body: str) -> Message:
    return Message(role="system", content=[Text(text=body)])


def _admit_system_context(
    epoch: ContextEpochTracker,
    ctx: TurnContext,
    chat_messages: Sequence[Message],
    *,
    memory_selection: MemoryPromptSelection | None = None,
    attachment_catalog: SessionAttachmentCatalog | None = None,
) -> EpochAdmit:
    """Compose stable/volatile tails and reconcile against the current context epoch."""
    catalog = (
        attachment_catalog.list_records() if attachment_catalog is not None else None
    )
    stable = compose_stable_baseline(ctx, attachment_catalog=catalog)
    volatile_parts = compose_volatile_tail_parts(
        ctx, chat_messages=chat_messages, memory_selection=memory_selection
    )
    volatile = "".join(volatile_parts.values())
    return epoch.reconcile(
        stable_baseline=stable,
        volatile_text=volatile,
        stable_fingerprint=fingerprint_text(stable),
        volatile_fingerprint=fingerprint_text(volatile),
        volatile_part_fingerprints={
            name: fingerprint_text(text) for name, text in volatile_parts.items()
        },
    )


def _messages_for_provider(
    system: Message,
    history: Sequence[Message],
    *,
    mid_conversation_update: str = "",
) -> list[Message]:
    """Leading system + history + optional chronological system-context update.

    The update is user-role so all providers accept mid-conversation updates
    (system is leading-only for Anthropic/Gemini/OpenAI adapters). When history
    already ends in a ``user`` row (e.g. a tool-response turn), the update is
    folded into that same message instead of appended as a new one — Anthropic
    and Gemini both reject (or, for Anthropic, only sometimes silently coalesce)
    two consecutive same-role messages.
    """
    out: list[Message] = [system, *list(history)]
    update = mid_conversation_update.strip()
    if not update:
        return out
    update_block = Text(text=mid_conversation_update)
    if out[-1].role == "user":
        out[-1] = Message(role="user", content=[*out[-1].content, update_block])
    else:
        out.append(Message(role="user", content=[update_block]))
    return out


async def _load_agent_chat_history(history: HistoryStore, thread_id: str) -> list[Message]:
    """Load transcript rows and apply agent-facing transforms (integrity + strip UI)."""
    return transform_context(await history.load(thread_id))


def _epoch_events(
    admit: EpochAdmit,
    *,
    request_id: str,
    thread_id: str,
) -> list[AgentEvent]:
    if admit.kind == "unchanged":
        return []
    logger.debug(
        "context epoch %s",
        kv(
            request_id=request_id,
            thread_id=thread_id,
            kind=admit.kind,
            epoch_id=admit.epoch_id,
            changed_sources=",".join(admit.changed_sources),
        ),
    )
    if admit.kind == "new_epoch":
        return [
            ContextEpochStarted(
                request_id=request_id,
                epoch_id=admit.epoch_id,
                changed_sources=list(admit.changed_sources),
            )
        ]
    return [
        SystemContextUpdated(
            request_id=request_id,
            epoch_id=admit.epoch_id,
            changed_sources=list(admit.changed_sources),
        )
    ]


def _provider_messages_prompt_summary(messages: Sequence[Message]) -> str:
    """Compact prompt text for observability (Langfuse ``gen_ai.prompt`` / observation input)."""
    lines: list[str] = []
    for msg in messages:
        parts: list[str] = []
        for block in msg.content:
            if isinstance(block, Text):
                parts.append(block.text)
            elif isinstance(block, ToolRequest):
                parts.append(f"[tool_call {block.name}]")
            elif isinstance(block, ToolResponse):
                parts.append(f"[tool_result {block.tool_name}]")
        text = " ".join(parts).strip()
        if text:
            lines.append(f"{msg.role}: {text}")
    return "\n".join(lines)


_HOOK_READ_TIMEOUT_S = 2.0
_HOOK_PRE_TOOL_TIMEOUT_S = 1.5
_HOOK_SETTLEMENT_TIMEOUT_S = 2.0


def _settlement_timeout_s() -> float:
    raw = os.environ.get("MONKEYBOT_HOOK_SETTLEMENT_TIMEOUT_S")
    if raw is None or raw.strip() == "":
        return _HOOK_SETTLEMENT_TIMEOUT_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _HOOK_SETTLEMENT_TIMEOUT_S


async def _drain_hook_settlement(hook_manager: HookManager | None) -> None:
    """Wait for fire-and-forget hooks before the next provider call / TurnComplete."""
    if hook_manager is None:
        return
    await hook_manager.drain_settlement(timeout_s=_settlement_timeout_s())


def _record_tool_hook_span_event(phase: str, tool_name: str) -> None:
    try:
        from monkeybot.observability.instrumentation import add_tool_hook_span_event

        if phase == "pre_tool":
            add_tool_hook_span_event(phase="pre_tool", tool_name=tool_name)
        else:
            add_tool_hook_span_event(phase="post_tool", tool_name=tool_name)
    except ImportError:
        return


async def _fire_hook(
    hook_manager: HookManager | None,
    *,
    event: HookEvent,
    ctx: TurnContext,
    timeout_s: float,
    user_message: str | None = None,
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
    tool_result: str | None = None,
    tool_error: str | None = None,
    tools: list[ToolDef] | None = None,
    provider_messages: list[Message] | None = None,
    inner_turn: int | None = None,
    assistant_text: str | None = None,
    thinking_text: str | None = None,
    tool_requests: list[dict[str, Any]] | None = None,
    usage: dict[str, int] | None = None,
    provider_error: str | None = None,
) -> HookPayload | None:
    """Construct + fire a payload when ``hook_manager`` is configured; else return ``None``."""
    if hook_manager is None:
        return None
    payload = HookPayload(
        event=event,
        thread_id=ctx.thread_id,
        request_id=ctx.request_id,
        ctx=ctx,
        user_message=user_message,
        tool_name=tool_name,
        tool_args=tool_args,
        tool_result=tool_result,
        tool_error=tool_error,
        tools=tools,
        provider_messages=provider_messages,
        inner_turn=inner_turn,
        assistant_text=assistant_text,
        thinking_text=thinking_text,
        tool_requests=tool_requests,
        usage=usage,
        provider_error=provider_error,
    )
    return await hook_manager.fire(payload, timeout_s=timeout_s)


def _apply_before_provider_hook(
    payload: HookPayload | None,
    provider_messages: list[Message],
    turn_tools: Sequence[ToolDef],
) -> tuple[list[Message], Sequence[ToolDef], bool]:
    """Return (messages, tools, tools_replaced) after ``BEFORE_PROVIDER_REQUEST``."""
    if payload is None:
        return provider_messages, turn_tools, False
    messages = (
        list(payload.provider_messages)
        if payload.provider_messages is not None
        else provider_messages
    )
    if payload.tools is None:
        return messages, turn_tools, False
    return messages, list(payload.tools), True


async def _fire_after_provider_response(
    hook_manager: HookManager | None,
    *,
    ctx: TurnContext,
    inner_turn: int,
    assistant_text: str | None = None,
    thinking_text: str | None = None,
    tool_requests: list[dict[str, Any]] | None = None,
    usage: dict[str, int] | None = None,
    provider_error: str | None = None,
) -> None:
    await _fire_hook(
        hook_manager,
        event=HookEvent.AFTER_PROVIDER_RESPONSE,
        ctx=ctx,
        timeout_s=0,
        inner_turn=inner_turn,
        assistant_text=assistant_text,
        thinking_text=thinking_text,
        tool_requests=tool_requests,
        usage=usage,
        provider_error=provider_error,
    )


def _append_extra_system_text(system: Message, extra: str | None) -> Message:
    """Return a new system Message with ``extra`` under a ``## Runtime notes`` section.

    Uses the same markdown heading style as the rest of the composed system prompt
    (``## Memory index``, harness sections). When ``extra`` is empty/None the original
    message is returned unchanged.
    """
    if not extra:
        return system
    base = "".join(b.text for b in system.content if isinstance(b, Text))
    wrapped = f"{base}{RUNTIME_NOTES_HEADING}\n{extra.strip()}\n"
    return Message(role="system", content=[Text(text=wrapped)])


def _combine_extras(*parts: str | None) -> str | None:
    """Join non-empty hook-injected fragments with blank lines; ``None`` if all empty."""
    kept = [p.strip() for p in parts if p and p.strip()]
    if not kept:
        return None
    return "\n\n".join(kept)


# Max concurrent ``task`` (subagent) subprocesses per single model tool batch.
_MAX_CONCURRENT_SUBAGENTS = 10
_MAX_CONCURRENT_PARALLEL_TOOLS = 10


def _parallel_tool_concurrency() -> int:
    raw = os.environ.get("MONKEYBOT_PARALLEL_TOOL_CONCURRENCY")
    if raw is None or raw.strip() == "":
        return _MAX_CONCURRENT_PARALLEL_TOOLS
    try:
        return max(1, int(raw))
    except ValueError:
        return _MAX_CONCURRENT_PARALLEL_TOOLS


def _parallel_safe_names(tools: Sequence[ToolDef]) -> frozenset[str]:
    """Names marked ``parallel_safe`` (read-only / concurrent-safe tools)."""
    return frozenset(t.name for t in tools if t.parallel_safe)

_SPILL_REL = Path(".monkeybot") / "spill"
SUMMARY_TRIGGER_RATIO = summarization_trigger_ratio_from_env()
"""Same ratio as pre-stream summarization check (``preflight_prompt_tokens >= cap``)."""
_SUMMARY_TRIGGER_RATIO = SUMMARY_TRIGGER_RATIO
_SUMMARY_KEEP_HEAD = 1
_SUMMARY_KEEP_TAIL = 6

# Fixed Markdown template for middle-history compression.
_COMPACTION_SUMMARY_SYSTEM = """\
Output exactly the Markdown structure shown inside <template> and keep the \
section order unchanged. Do not include the <template> tags in your response.
<template>
## Objective
- [one or two brief sentences describing what the user is trying to accomplish]

## Important Details
- [constraints/preferences, decisions and why, important facts/assumptions, \
exact context needed to continue, or "(none)"]

## Work State
### Completed
- [finished work, verified facts, or changes made; otherwise "(none)"]

### Active
- [current work, partial changes, or investigation state; otherwise "(none)"]

### Blocked
- [blockers, failing commands, or unknowns; otherwise "(none)"]

## Next Move
1. [immediate concrete action, or "(none)"]
2. [next action if known, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]
</template>

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, symbols, commands, error strings, URLs, and \
identifiers when known.
- Do not mention the summary process or that context was compacted.\
"""


async def _await_user_response_any(
    bus: object,
    fut: asyncio.Future[Any],
    pending_key: str,
    *,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Wait for POST/resolve on ``fut`` with optional bus timeout bookkeeping.

    Used by the gateway :func:`_await_user_response` and the inspector confirm path.
    """
    t = timeout_sec if timeout_sec is not None else float(os.environ.get("PENDING_RESPONSE_TIMEOUT_SEC", "300"))
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout=t)
    except TimeoutError:
        ap = getattr(bus, "abandon_pending_timeout", None)
        if callable(ap):
            ap(pending_key)
        return {"_timeout": True}


def _flatten_tool_result_for_summary(resp: ToolResponse) -> str:
    parts: list[str] = []
    for b in resp.result:
        if isinstance(b, Text):
            parts.append(summarize_tool_result_text(b.text))
        else:
            parts.append(summarize_tool_result_text(json.dumps(b.to_dict(), sort_keys=True)))
    return "".join(parts) or "(empty)"


def _summary_line_for_message(m: Message) -> str:
    pieces: list[str] = []
    for b in m.content:
        if isinstance(b, Text):
            pieces.append(b.text)
        elif isinstance(b, ToolRequest):
            pieces.append(f"[tool_call: {b.name}({json.dumps(b.args, sort_keys=True)})]")
        elif isinstance(b, ToolResponse):
            body = _flatten_tool_result_for_summary(b)
            tag = "tool_error" if b.is_error else "tool_result"
            pieces.append(f"[{tag} {b.tool_name}: {body}]")
        else:
            pieces.append(f"[{type(b).__name__}]")
    joined = " ".join(pieces) if pieces else "(empty)"
    return f"{m.role}: {joined}"


def _system_prompt_snapshot_text(
    system: Message, mid_conversation_update: str = ""
) -> str:
    """Plain string for :class:`SystemPromptSnapshot` (composed prompt + mid-epoch update)."""
    body = "".join(b.text for b in system.content if isinstance(b, Text))
    update = mid_conversation_update.strip()
    if not update:
        return body
    return f"{body}\n\n{update}"


def _is_resume_turn(resolved_messages: Sequence[Message]) -> bool:
    """True when the model continues after tool results, not a new user question."""
    if not resolved_messages:
        return False
    last = resolved_messages[-1]
    if last.role != "user":
        return False
    has_tool_response = any(isinstance(b, ToolResponse) for b in last.content)
    has_user_text = any(isinstance(b, Text) and b.text.strip() for b in last.content)
    return has_tool_response and not has_user_text


def _is_routine_resume_turn(resolved_messages: Sequence[Message]) -> bool:
    """Resume turn with only successful tool results (safe to reduce thinking budget)."""
    if not _is_resume_turn(resolved_messages):
        return False
    last = resolved_messages[-1]
    tool_responses = [b for b in last.content if isinstance(b, ToolResponse)]
    return bool(tool_responses) and all(not b.is_error for b in tool_responses)


def _stream_thinking_budget(
    provider: Provider,
    resolved_messages: Sequence[Message],
) -> int | None:
    """Per-call thinking budget override; None keeps the provider default."""
    if provider.name not in ("gemini", "claude", "ollama"):
        return None
    raw = os.environ.get("MONKEYBOT_RESUME_THINKING_BUDGET", "").strip()
    if not raw:
        return None
    try:
        resume_budget = int(raw)
    except ValueError:
        return None
    if _is_routine_resume_turn(resolved_messages):
        return resume_budget
    return None


async def _provider_prompt_input_tokens(
    provider: Provider,
    messages: Sequence[Message],
    tools: Sequence[ToolDef],
    *,
    model: str,
    thinking_budget: int | None = None,
    vertex_google_search: bool = False,
    hints: ProviderCallHints | None = None,
) -> int:
    return await provider_count_input_tokens(
        provider,
        messages,
        tools,
        model=model,
        thinking_budget=thinking_budget,
        vertex_google_search=vertex_google_search,
        hints=hints,
    )


def _provider_call_hints(ctx: TurnContext) -> ProviderCallHints:
    return ProviderCallHints(
        session_id=ctx.thread_id,
        cache_retention=cache_retention_from_env(),
    )


async def _prompt_input_tokens_for_history(
    *,
    ctx: TurnContext,
    chat_messages: Sequence[Message],
    provider: Provider,
    attachment_store: AttachmentStore | None,
    attachment_catalog: SessionAttachmentCatalog | None,
    memory_selection: MemoryPromptSelection | None = None,
    extra_system_text: str | None = None,
    vertex_google_search: bool = False,
    epoch: ContextEpochTracker | None = None,
) -> int:
    """Provider-accurate prompt size for history rows already persisted (e.g. post-assistant).

    When ``epoch`` is supplied, the count reflects the true wire shape for the
    *current* epoch state — leading (cached) baseline plus any pending
    mid-conversation update — via a non-mutating :meth:`ContextEpochTracker.peek`.
    Falls back to a flat stable+volatile concatenation when no tracker is given
    (e.g. summarizer/curator calls that have no epoch of their own). Either way,
    the live tracker is never mutated by a budget recount.
    """
    catalog = (
        attachment_catalog.list_records() if attachment_catalog is not None else None
    )
    stable = compose_stable_baseline(ctx, attachment_catalog=catalog)
    volatile = compose_volatile_tail(
        ctx, chat_messages=chat_messages, memory_selection=memory_selection
    )
    mid_conversation_update = ""
    if epoch is not None:
        admit = epoch.peek(
            stable_baseline=stable,
            volatile_text=volatile,
            stable_fingerprint=fingerprint_text(stable),
            volatile_fingerprint=fingerprint_text(volatile),
        )
        body = admit.leading_system_text
        mid_conversation_update = admit.mid_conversation_update
    else:
        body = stable + volatile
    system = _append_extra_system_text(_system_message_from_text(body), extra_system_text)
    resolved_messages = convert_to_provider(
        chat_messages,
        attachment_store=attachment_store,
        session_id=ctx.thread_id,
    )
    provider_messages = _messages_for_provider(
        system, resolved_messages, mid_conversation_update=mid_conversation_update
    )
    return await _provider_prompt_input_tokens(
        provider, provider_messages, ctx.tools, model=ctx.model,
        vertex_google_search=vertex_google_search,
        hints=_provider_call_hints(ctx),
    )


def _cleanup_spill_files(workspace_root: Path, thread_id: str) -> None:
    spill_path = Path(workspace_root).resolve() / _SPILL_REL / thread_id
    if spill_path.exists():
        shutil.rmtree(spill_path, ignore_errors=True)


def _summarization_viable(messages: Sequence[Message]) -> bool:
    return len(messages) > _SUMMARY_KEEP_HEAD + _SUMMARY_KEEP_TAIL


def _summarization_model_id(ctx: TurnContext) -> str:
    """Model id for history compression; env overrides ``ctx.summarization_model``, then ``ctx.model``."""
    from_env = os.getenv("CONTEXT_SUMMARIZATION_MODEL", "").strip()
    if from_env:
        return from_env
    ctx_sm = (ctx.summarization_model or "").strip()
    if ctx_sm:
        return ctx_sm
    return ctx.model


async def _compact_history_if_needed(
    *,
    thread_id: str,
    history: HistoryStore,
    provider: Provider,
    model: str,
) -> int:
    """Summarize middle history when tool results exhausted headroom."""
    # Compaction persists via history.reset; use unrepaired rows so synthetic
    # in-memory repairs are never written to the store.
    chat_messages = await history.load(thread_id)
    if not _summarization_viable(chat_messages):
        return 0
    return await _summarize_history(thread_id, chat_messages, history, provider, model)


async def _append_budgeted_tool_responses(
    *,
    chunk_responses: list[ContentBlock],
    ctx: TurnContext,
    history: HistoryStore,
    usage: Usage,
    budgeter: ContextBudgeter,
    provider: Provider,
    vertex_google_search: bool = False,
    epoch: ContextEpochTracker | None = None,
) -> AsyncIterator[AgentEvent]:
    """Budget tool results against remaining context, append, compact if needed."""
    trimmed, needs_compaction = budgeter.fit_content_blocks(chunk_responses)
    await history.append(
        ctx.thread_id,
        Message(role="user", content=trimmed),
    )
    usage.estimated_prompt_tokens = max(usage.estimated_prompt_tokens, budgeter.used_tokens)
    if not needs_compaction:
        return
    yield ContextSummarizing(
        request_id=ctx.request_id,
        estimated_tokens=usage.estimated_prompt_tokens,
        context_window_tokens=ctx.context_window_tokens,
    )
    try:
        turns_summarized = await _compact_history_if_needed(
            thread_id=ctx.thread_id,
            history=history,
            provider=provider,
            model=_summarization_model_id(ctx),
        )
    except Exception:
        logger.warning(
            "post-tool summarization failed %s; continuing",
            kv(request_id=ctx.request_id, thread_id=ctx.thread_id),
            exc_info=True,
        )
        turns_summarized = 0
    yield ContextSummarized(
        request_id=ctx.request_id,
        turns_summarized=turns_summarized,
    )
    if turns_summarized > 0:
        if epoch is not None:
            epoch.begin_new_epoch()
        chat_messages = await _load_agent_chat_history(history, ctx.thread_id)
        post = await _prompt_input_tokens_for_history(
            ctx=ctx,
            chat_messages=chat_messages,
            provider=provider,
            attachment_store=None,
            attachment_catalog=None,
            vertex_google_search=vertex_google_search,
            epoch=epoch,
        )
        usage.estimated_prompt_tokens = max(usage.estimated_prompt_tokens, post)


async def _summarize_history(
    thread_id: str,
    messages: list[Message],
    history: HistoryStore,
    provider: Provider,
    model: str,
) -> int:
    """Compress middle history into one assistant summary row. Returns middle row count."""
    if not _summarization_viable(messages):
        return 0
    head = messages[:_SUMMARY_KEEP_HEAD]
    tail = messages[-_SUMMARY_KEEP_TAIL :]
    middle = messages[_SUMMARY_KEEP_HEAD : -_SUMMARY_KEEP_TAIL]
    if not middle:
        return 0
    lines = [_summary_line_for_message(m) for m in middle]
    blob = "\n\n---\n\n".join(lines)
    summarize_messages = [
        Message(
            role="system",
            content=[Text(text=_COMPACTION_SUMMARY_SYSTEM)],
        ),
        Message(
            role="user",
            content=[
                Text(
                    text=(
                        "Create a structured summary from the conversation "
                        "segment below.\n\n" + blob
                    )
                )
            ],
        ),
    ]
    summary_text = ""
    async with aclosing(
        cast(Any, provider.stream(summarize_messages, [], model=model))
    ) as stream:
        async for ev in stream:
            if isinstance(ev, TextDelta):
                summary_text += ev.text
            elif isinstance(ev, Done):
                break
    summary_text = summary_text.strip() or "(empty summary)"
    merged = [
        *head,
        Message(
            role="assistant",
            content=[Text(text=f"[Context Summary]:\n{summary_text}")],
        ),
        *tail,
    ]
    await history.reset(thread_id, merged)
    return len(middle)


def _tool_outcome(
    call: ToolCall,
    request_id: str,
    result: ToolExecutionResult,
) -> tuple[ToolCallResult, ToolResponse]:
    """Build the (event, history block) pair for a finished tool call.

    Used by both the sequential and parallel ``task`` dispatch paths so that
    result formatting stays in one place.
    """
    is_error = result.error is not None
    body = "" if is_error else _blocks_to_sse_summary(result.blocks)
    text = result.error if is_error else body
    response_blocks: list[ContentBlock] = (
        list(result.blocks) if not is_error else [Text(text=text or "")]
    )
    if not is_error:
        budget = resolve_tool_budget(call.name)
        if budget is not None:
            flat = _blocks_to_sse_summary(response_blocks)
            if exceeds_tool_output_budget(flat, tool_name=call.name, budget=budget):
                shaped_blocks: list[ContentBlock] = []
                for block in response_blocks:
                    if isinstance(block, Text):
                        shaped = shape_tool_text(
                            block.text,
                            tool_name=call.name,
                            budget=budget,
                            pressure_tier=None,
                        )
                        shaped_blocks.append(
                            Text(text=shaped) if shaped != block.text else block
                        )
                    else:
                        shaped_blocks.append(block)
                response_blocks = shaped_blocks
                body = _blocks_to_sse_summary(response_blocks)
    event = ToolCallResult(
        request_id=request_id,
        tool=call.name,
        result=body,
        error=result.error,
        call_id=call.call_id,
    )
    response = ToolResponse(
        id=call.call_id,
        tool_name=call.name,
        result=response_blocks,
        is_error=is_error,
    )
    return event, response


def _image_events(
    request_id: str,
    call_id: str,
    result: ToolExecutionResult,
) -> list[ImageBlock]:
    """SSE image payloads for tool results that include ``Image`` content blocks."""
    if result.error is not None:
        return []
    events: list[ImageBlock] = []
    for idx, b in enumerate(result.blocks):
        if not isinstance(b, Image) or not b.data:
            continue
        image_id = f"{call_id}:{idx}" if call_id else f"{request_id}:{idx}"
        events.append(
            ImageBlock(
                request_id=request_id,
                image_id=image_id,
                mime_type=b.mime_type,
                data=b.data,
            )
        )
    return events


def _chunk_tool_calls(
    ordered: Sequence[ToolCall],
    *,
    parallel_safe: frozenset[str] | None = None,
) -> list[list[ToolCall]]:
    """Split into maximal runs of consecutive parallelizable tools vs serial singles.

    * Consecutive ``task`` calls may run concurrently (bounded by
      :data:`_MAX_CONCURRENT_SUBAGENTS`).
    * Consecutive tools in ``parallel_safe`` (read-only) may run concurrently
      (bounded by :func:`_parallel_tool_concurrency`).
    * ``task`` never mixes with other parallel-safe tools in one chunk.
    * Mutating / unmarked tools stay sequential chunk-by-chunk.
    """
    safe = parallel_safe or frozenset()
    seq = list(ordered)
    chunks: list[list[ToolCall]] = []
    i = 0
    n = len(seq)
    while i < n:
        name = seq[i].name
        if name == "task":
            j = i + 1
            while j < n and seq[j].name == "task":
                j += 1
            chunks.append(seq[i:j])
            i = j
        elif name in safe:
            j = i + 1
            while j < n and seq[j].name in safe and seq[j].name != "task":
                j += 1
            chunks.append(seq[i:j])
            i = j
        else:
            chunks.append([seq[i]])
            i += 1
    return chunks


@runtime_checkable
class ToolExecutorPort(Protocol):
    """Fakeable tool execution boundary (Story 6 does not invoke real shell)."""

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
        """Return content blocks for history; ``error`` set on failure."""


async def run(
    user_content: str | list[ContentBlock],
    ctx: TurnContext,
    *,
    provider: Provider,
    history: HistoryStore,
    inspectors: list[ToolInspector],
    tool_executor: ToolExecutorPort,
    cancelled: asyncio.Event | None = None,
    max_turns: int | None = None,
    hook_manager: HookManager | None = None,
    curator_provider: Provider | None = None,
    attachment_store: AttachmentStore | None = None,
    attachment_catalog: SessionAttachmentCatalog | None = None,
    transcript_writer: TranscriptWriter | None = None,
    vertex_google_search: bool = False,
    input_admission: InputAdmission | None = None,
) -> AsyncIterator[AgentEvent]:
    """Stream agent events for one user message; ends with ``TurnComplete`` (never raises).

    Provider chunks are handled in Gemini-style batches: tool calls accumulate until ``Done``,
    then execute in lexicographic ``call_id`` order for deterministic replay.

    Consecutive ``task`` tool calls and consecutive ``parallel_safe`` tools in one batch run
    concurrently (capped); mutating tools run one chunk at a time in order.

    ``input_admission`` (optional) supplies mid-turn steer injections drained at safe
    boundaries (after tool batches / before the next provider call).

    ``curator_provider`` is an optional dedicated provider for context curation (e.g. with
    ``thinking_budget=0``). Falls back to ``provider`` when not supplied.

    ``vertex_google_search`` opts into Gemini native ``google_search`` grounding for agent
    turns in this run (main agent or subagent). Only passed through to ``GeminiProvider``;
    internal harness calls (history summarization, memory organizer) never set this flag.
    """
    usage = Usage()
    t0 = time.monotonic()
    trace_id_capture: list[str | None] = [None]
    blocks = _normalize_user_content(user_content)
    effective_max = _effective_max_turns(max_turns)
    logger.debug(
        "harness run start %s",
        kv(
            request_id=ctx.request_id,
            thread_id=ctx.thread_id,
            model=ctx.model,
            max_turns=effective_max,
        ),
    )
    try:
        async for evt in _run_inner(
            blocks,
            ctx,
            provider=provider,
            history=history,
            inspectors=inspectors,
            tool_executor=tool_executor,
            cancelled=cancelled,
            max_turns=max_turns,
            usage=usage,
            hook_manager=hook_manager,
            curator_provider=curator_provider,
            trace_id_out=trace_id_capture,
            attachment_store=attachment_store,
            attachment_catalog=attachment_catalog,
            transcript_writer=transcript_writer,
            vertex_google_search=vertex_google_search,
            input_admission=input_admission,
        ):
            yield evt
    except asyncio.CancelledError:
        try:
            cur = asyncio.current_task()
            if cur is not None and getattr(cur, "uncancel", None):
                cur.uncancel()
        except Exception:
            logger.warning(
                "uncancel cleanup failed %s",
                kv(request_id=ctx.request_id, thread_id=ctx.thread_id),
                exc_info=True,
            )
        yield Error(request_id=ctx.request_id, error="Request cancelled")
    except Exception as exc:
        logger.exception(
            "harness turn failed %s",
            kv(request_id=ctx.request_id, thread_id=ctx.thread_id),
        )
        yield Error(request_id=ctx.request_id, error=str(exc))
    finally:
        await _drain_hook_settlement(hook_manager)
        usage.duration_ms = int((time.monotonic() - t0) * 1000)
        logger.debug(
            "harness run end %s",
            kv(
                request_id=ctx.request_id,
                thread_id=ctx.thread_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                duration_ms=usage.duration_ms,
            ),
        )
        yield TurnComplete(
            request_id=ctx.request_id,
            usage=_usage_to_totals(usage),
            trace_id=trace_id_capture[0],
        )


async def _run_inner(
    user_content: list[ContentBlock],
    ctx: TurnContext,
    *,
    provider: Provider,
    history: HistoryStore,
    inspectors: list[ToolInspector],
    tool_executor: ToolExecutorPort,
    cancelled: asyncio.Event | None,
    max_turns: int | None,
    usage: Usage,
    hook_manager: HookManager | None = None,
    curator_provider: Provider | None = None,
    trace_id_out: list[str | None] | None = None,
    attachment_store: AttachmentStore | None = None,
    attachment_catalog: SessionAttachmentCatalog | None = None,
    transcript_writer: TranscriptWriter | None = None,
    vertex_google_search: bool = False,
    input_admission: InputAdmission | None = None,
) -> AsyncIterator[AgentEvent]:
    from monkeybot.observability.spans import set_run_output, span_run

    last_assistant: list[str] = [""]
    user_text = _user_text_from_content(user_content)
    async with span_run(ctx, user_message=user_text):
        async for evt in _run_inner_core(
            user_content,
            ctx,
            provider=provider,
            history=history,
            inspectors=inspectors,
            tool_executor=tool_executor,
            cancelled=cancelled,
            max_turns=max_turns,
            usage=usage,
            hook_manager=hook_manager,
            curator_provider=curator_provider,
            last_assistant=last_assistant,
            attachment_store=attachment_store,
            attachment_catalog=attachment_catalog,
            transcript_writer=transcript_writer,
            vertex_google_search=vertex_google_search,
            input_admission=input_admission,
        ):
            yield evt
        set_run_output(last_assistant[0])
        if trace_id_out is not None:
            try:
                from monkeybot.observability import is_observability_enabled
                from monkeybot.observability.instrumentation import (
                    get_current_trace_id_hex_optional,
                )

                if is_observability_enabled():
                    trace_id_out[0] = get_current_trace_id_hex_optional()
            except ImportError:
                pass


async def _run_inner_core(
    user_content: list[ContentBlock],
    ctx: TurnContext,
    *,
    provider: Provider,
    history: HistoryStore,
    inspectors: list[ToolInspector],
    tool_executor: ToolExecutorPort,
    cancelled: asyncio.Event | None,
    max_turns: int | None,
    usage: Usage,
    hook_manager: HookManager | None = None,
    curator_provider: Provider | None = None,
    last_assistant: list[str],
    attachment_store: AttachmentStore | None = None,
    attachment_catalog: SessionAttachmentCatalog | None = None,
    transcript_writer: TranscriptWriter | None = None,
    vertex_google_search: bool = False,
    input_admission: InputAdmission | None = None,
) -> AsyncIterator[AgentEvent]:
    from monkeybot.observability.spans import (
        begin_turn_span,
        end_turn_span,
        record_tool_outcome,
        set_llm_io,
        set_llm_usage,
        set_summarize_turns,
        set_turn_io,
        set_turn_prompt_tokens,
        span_llm,
        span_summarize,
        span_tool,
    )

    effective_max = _effective_max_turns(max_turns)
    user_text = _user_text_from_content(user_content)
    _ = await history.load(ctx.thread_id)
    if ctx.workspace_root is not None:
        _cleanup_spill_files(ctx.workspace_root, ctx.thread_id)
    await history.append(ctx.thread_id, Message(role="user", content=list(user_content)))

    await _fire_hook(
        hook_manager,
        event=HookEvent.USER_MESSAGE,
        ctx=ctx,
        timeout_s=0,
        user_message=user_text,
    )

    turn_index = 0
    needs_followup_after_tools = False
    provider_messages_written = 0
    tools_dirty = False
    # Final assistant write is backgrounded off the streaming path; awaited at the
    # turn tail before any history load/reset so the row is never lost.
    assistant_write_task: asyncio.Task[None] | None = None
    memory_selection: MemoryPromptSelection | None = None
    memory_selection_fingerprint: str | None = None
    pre_turn_extra: str | None = None
    pre_tool_extra_next: str | None = None
    doom_tracker = _DoomLoopTracker(
        threshold=_effective_doom_loop_threshold(),
        exempt_names=_doom_loop_exempt_names(ctx.tools),
    )
    epoch_tracker = ContextEpochTracker()

    def _finish_tool(
        call: ToolCall,
        result: ToolExecutionResult,
    ) -> tuple[ToolCallResult, ToolResponse]:
        event, response = _tool_outcome(call, ctx.request_id, result)
        doom_tracker.record(call.name, dict(call.args))
        return event, response

    async def _drain_steers() -> AsyncIterator[AgentEvent]:
        """Inject queued steer messages at a safe boundary (before next provider call)."""
        if input_admission is None:
            return
        while True:
            steered = input_admission.pop_steer()
            if steered is None:
                break
            await history.append(
                ctx.thread_id, Message(role="user", content=list(steered))
            )
            preview = preview_text(steered)
            logger.info(
                "steer injected %s",
                kv(
                    request_id=ctx.request_id,
                    thread_id=ctx.thread_id,
                    preview=preview[:80],
                ),
            )
            yield UserSteered(request_id=ctx.request_id, text=preview)

    while turn_index < effective_max:
        if cancelled is not None and cancelled.is_set():
            yield Error(request_id=ctx.request_id, error="Request cancelled")
            needs_followup_after_tools = False
            break

        async for steer_evt in _drain_steers():
            yield steer_evt

        # Settlement barrier: fire-and-forget hooks from the prior tool batch
        # must finish (or time out) before the next provider call.
        await _drain_hook_settlement(hook_manager)

        turn_index += 1
        turn_input_text = user_text
        turn_output_text = ""
        logger.debug(
            "inner turn start %s",
            kv(
                request_id=ctx.request_id,
                thread_id=ctx.thread_id,
                turn=turn_index,
                estimated_prompt_tokens=usage.estimated_prompt_tokens,
            ),
        )
        turn_span = begin_turn_span(
            turn_index=turn_index,
            thread_id=ctx.thread_id,
            request_id=ctx.request_id,
        )
        try:
            yield Thinking(request_id=ctx.request_id)

            if cancelled is not None and cancelled.is_set():
                yield Error(request_id=ctx.request_id, error="Request cancelled")
                needs_followup_after_tools = False
                break

            chat_messages = await _load_agent_chat_history(history, ctx.thread_id)
            ctx = await refresh_memory_index(ctx)
            turn_input_text = latest_user_message_text(chat_messages) or user_text
            set_turn_io(input_value=turn_input_text)

            if turn_index == 1:
                pre_turn_payload = await _fire_hook(
                    hook_manager,
                    event=HookEvent.PRE_TURN,
                    ctx=ctx,
                    timeout_s=_HOOK_READ_TIMEOUT_S,
                    user_message=user_text,
                )
                if pre_turn_payload is not None:
                    pre_turn_extra = pre_turn_payload.inject_text
                    if pre_turn_payload.inject_memory_lines:
                        ctx = dataclasses.replace(
                            ctx,
                            memory_index=[
                                *ctx.memory_index,
                                *pre_turn_payload.inject_memory_lines,
                            ],
                        )

            index_fp = memory_index_fingerprint(ctx.memory_index)
            if memory_selection is None or memory_selection_fingerprint != index_fp:
                u = latest_user_message_text(chat_messages) or user_text
                memory_selection = await prepare_memory_for_prompt(
                    ctx=ctx,
                    user_message=u,
                    provider=provider,
                    curator_provider=curator_provider,
                )
                memory_selection_fingerprint = index_fp
                logger.debug(
                    "memory prompt selection %s",
                    kv(
                        request_id=ctx.request_id,
                        thread_id=ctx.thread_id,
                        turn=turn_index,
                        memory_lines=len(memory_selection.lines),
                        total_lines=memory_selection.total_lines,
                        coverage=memory_selection.coverage,
                        confidence=memory_selection.confidence,
                        nudge_search=memory_selection.nudge_search,
                    ),
                )

            admit = _admit_system_context(
                epoch_tracker,
                ctx,
                chat_messages,
                memory_selection=memory_selection,
                attachment_catalog=attachment_catalog,
            )
            for epoch_evt in _epoch_events(
                admit, request_id=ctx.request_id, thread_id=ctx.thread_id
            ):
                yield epoch_evt
            system = _system_message_from_text(admit.leading_system_text)
            combined_extra = _combine_extras(pre_turn_extra, pre_tool_extra_next)
            force_no_tools, doom_loop_note = doom_tracker.consume_recovery()
            combined_extra = _combine_extras(combined_extra, doom_loop_note)
            system = _append_extra_system_text(system, combined_extra)
            pre_tool_extra_next = None
            turn_tools: Sequence[ToolDef] = () if force_no_tools else ctx.tools
            tool_def_payload = await _fire_hook(
                hook_manager,
                event=HookEvent.TOOL_DEFINITION,
                ctx=ctx,
                timeout_s=_HOOK_READ_TIMEOUT_S,
                tools=list(turn_tools),
                inner_turn=turn_index,
            )
            if tool_def_payload is not None and tool_def_payload.tools is not None:
                turn_tools = list(tool_def_payload.tools)
                tools_dirty = True
                logger.debug(
                    "tool.definition hook replaced tools %s",
                    kv(
                        request_id=ctx.request_id,
                        thread_id=ctx.thread_id,
                        turn=turn_index,
                        tool_count=len(turn_tools),
                    ),
                )
            # Preflight uses unshaped history; pressure shaping applied after token count.
            resolved_messages = convert_to_provider(
                chat_messages,
                attachment_store=attachment_store,
                session_id=ctx.thread_id,
            )
            provider_messages = _messages_for_provider(
                system,
                resolved_messages,
                mid_conversation_update=admit.mid_conversation_update,
            )

            stream_thinking = _stream_thinking_budget(provider, resolved_messages)
            preflight = await _provider_prompt_input_tokens(
                provider,
                provider_messages,
                turn_tools,
                model=ctx.model,
                thinking_budget=stream_thinking,
                vertex_google_search=vertex_google_search,
                hints=_provider_call_hints(ctx),
            )
            usage.estimated_prompt_tokens = max(usage.estimated_prompt_tokens, preflight)
            cap = max(1, int(ctx.context_window_tokens * _SUMMARY_TRIGGER_RATIO))
            if preflight >= cap and _summarization_viable(chat_messages):
                logger.debug(
                    "summarization triggered %s",
                    kv(
                        request_id=ctx.request_id,
                        thread_id=ctx.thread_id,
                        turn=turn_index,
                        preflight=preflight,
                        cap=cap,
                    ),
                )
                yield ContextSummarizing(
                    request_id=ctx.request_id,
                    estimated_tokens=preflight,
                    context_window_tokens=ctx.context_window_tokens,
                )
                async with span_summarize(
                    thread_id=ctx.thread_id,
                    request_id=ctx.request_id,
                ):
                    try:
                        turns_summarized = await _summarize_history(
                            ctx.thread_id,
                            chat_messages,
                            history,
                            provider,
                            _summarization_model_id(ctx),
                        )
                    except Exception:
                        logger.warning(
                            "summarization failed %s; continuing",
                            kv(request_id=ctx.request_id, thread_id=ctx.thread_id),
                            exc_info=True,
                        )
                        turns_summarized = 0
                    set_summarize_turns(turns_summarized)
                yield ContextSummarized(
                    request_id=ctx.request_id,
                    turns_summarized=turns_summarized,
                )
                logger.debug(
                    "summarization done %s",
                    kv(
                        request_id=ctx.request_id,
                        thread_id=ctx.thread_id,
                        turn=turn_index,
                        turns_summarized=turns_summarized,
                    ),
                )
                if turns_summarized > 0:
                    epoch_tracker.begin_new_epoch()
                chat_messages = await _load_agent_chat_history(history, ctx.thread_id)
                ctx = await refresh_memory_index(ctx)
                admit = _admit_system_context(
                    epoch_tracker,
                    ctx,
                    chat_messages,
                    memory_selection=memory_selection,
                    attachment_catalog=attachment_catalog,
                )
                for epoch_evt in _epoch_events(
                    admit, request_id=ctx.request_id, thread_id=ctx.thread_id
                ):
                    yield epoch_evt
                system = _append_extra_system_text(
                    _system_message_from_text(admit.leading_system_text),
                    pre_turn_extra,
                )
                resolved_messages = convert_to_provider(
                    chat_messages,
                    attachment_store=attachment_store,
                    session_id=ctx.thread_id,
                )
                provider_messages = _messages_for_provider(
                    system,
                    resolved_messages,
                    mid_conversation_update=admit.mid_conversation_update,
                )
                post = await _provider_prompt_input_tokens(
                    provider,
                    provider_messages,
                    turn_tools,
                    model=ctx.model,
                    thinking_budget=_stream_thinking_budget(provider, resolved_messages),
                    vertex_google_search=vertex_google_search,
                    hints=_provider_call_hints(ctx),
                )
                usage.estimated_prompt_tokens = max(usage.estimated_prompt_tokens, post)

            pressure_tier = compute_context_pressure_tier(
                usage.estimated_prompt_tokens,
                ctx.context_window_tokens,
            )
            if pressure_tier in ("moderate", "aggressive"):
                resolved_messages = convert_to_provider(
                    chat_messages,
                    attachment_store=attachment_store,
                    session_id=ctx.thread_id,
                    pressure_tier=pressure_tier,
                    protect_recent=_SUMMARY_KEEP_TAIL,
                )
                provider_messages = _messages_for_provider(
                    system,
                    resolved_messages,
                    mid_conversation_update=admit.mid_conversation_update,
                )

            before_payload = await _fire_hook(
                hook_manager,
                event=HookEvent.BEFORE_PROVIDER_REQUEST,
                ctx=ctx,
                timeout_s=_HOOK_READ_TIMEOUT_S,
                tools=list(turn_tools),
                provider_messages=list(provider_messages),
                inner_turn=turn_index,
            )
            prev_msg_count = len(provider_messages)
            provider_messages, turn_tools, tools_replaced = _apply_before_provider_hook(
                before_payload, provider_messages, turn_tools
            )
            if tools_replaced:
                tools_dirty = True
            if before_payload is not None and (
                tools_replaced
                or before_payload.provider_messages is not None
            ):
                logger.debug(
                    "before_provider_request hook rewrote request %s",
                    kv(
                        request_id=ctx.request_id,
                        thread_id=ctx.thread_id,
                        turn=turn_index,
                        tools_replaced=tools_replaced,
                        message_count=len(provider_messages),
                        prev_message_count=prev_msg_count,
                    ),
                )

            yield SystemPromptSnapshot(
                request_id=ctx.request_id,
                inner_turn=turn_index,
                text=_system_prompt_snapshot_text(system, admit.mid_conversation_update),
            )

            llm_input = 0
            llm_output = 0
            llm_cached = 0
            llm_cache_read = 0
            llm_cache_creation = 0
            if transcript_writer is not None:
                if provider_messages_written >= len(provider_messages):
                    delta_messages = provider_messages
                    message_offset = 0
                    messages_reset = provider_messages_written > 0
                else:
                    delta_messages = provider_messages[provider_messages_written:]
                    message_offset = provider_messages_written
                    messages_reset = False
                include_tools = turn_index == 1 or messages_reset or tools_dirty
                await transcript_writer.write_provider_request(
                    request_id=ctx.request_id,
                    inner_turn=turn_index,
                    model=ctx.model,
                    messages=[m.to_dict() for m in delta_messages],
                    message_offset=message_offset,
                    messages_reset=messages_reset,
                    tools=[dataclasses.asdict(t) for t in turn_tools] if include_tools else None,
                    thinking_budget=stream_thinking,
                )
                provider_messages_written = len(provider_messages)
                tools_dirty = False
            stream_mapper = ProviderStreamMapper(ctx.request_id)
            try:
                async with span_llm(ctx=ctx, vertex_google_search=vertex_google_search):
                    async with aclosing(
                        cast(
                            Any,
                            provider_stream(
                                provider,
                                provider_messages,
                                turn_tools,
                                model=ctx.model,
                                thinking_budget=stream_thinking,
                                vertex_google_search=vertex_google_search,
                                hints=_provider_call_hints(ctx),
                            ),
                        )
                    ) as stream:
                        async for ev in stream:
                            if isinstance(ev, UsageEvent):
                                _merge_usage_event(usage, ev)
                                usage.cost_usd += estimate_cost(
                                    ctx.model,
                                    ev.input_tokens,
                                    ev.output_tokens,
                                    cache_read_tokens=ev.cache_read_tokens,
                                    cache_creation_tokens=ev.cache_creation_tokens,
                                )
                                llm_input += ev.input_tokens
                                llm_output += ev.output_tokens
                                llm_cached += ev.cached_tokens
                                llm_cache_read += ev.cache_read_tokens
                                llm_cache_creation += ev.cache_creation_tokens
                                continue
                            if isinstance(ev, Done):
                                for aev in stream_mapper.map(ev):
                                    yield aev
                                break
                            for aev in stream_mapper.map(ev):
                                yield aev
                    for aev in stream_mapper.finish():
                        yield aev
                    pending = stream_mapper.pending
                    assistant_text = stream_mapper.assistant_text
                    thinking_text = stream_mapper.thinking_text
                    thinking_signature = stream_mapper.thinking_signature
                    stream_truncated = stream_mapper.stream_truncated
                    set_llm_usage(
                        input_tokens=llm_input,
                        output_tokens=llm_output,
                        cached_tokens=llm_cached,
                        cache_read_tokens=llm_cache_read,
                        cache_creation_tokens=llm_cache_creation,
                    )
                    set_llm_io(
                        prompt=_provider_messages_prompt_summary(provider_messages),
                        completion=assistant_text or "",
                    )
                if transcript_writer is not None:
                    await transcript_writer.write_provider_response(
                        request_id=ctx.request_id,
                        inner_turn=turn_index,
                        model=ctx.model,
                        text=assistant_text,
                        thinking=thinking_text,
                        tool_requests=[
                            {"call_id": tc.call_id, "name": tc.name, "args": tc.args}
                            for tc in pending.values()
                        ],
                        usage={
                            "input_tokens": llm_input,
                            "output_tokens": llm_output,
                            "cached_tokens": llm_cached,
                            "cache_read_tokens": llm_cache_read,
                            "cache_creation_tokens": llm_cache_creation,
                        },
                    )
                logger.debug(
                    "provider stream done %s",
                    kv(
                        request_id=ctx.request_id,
                        thread_id=ctx.thread_id,
                        turn=turn_index,
                        llm_input=llm_input,
                        llm_output=llm_output,
                        llm_cached=llm_cached,
                        tool_calls=len(pending),
                    ),
                )
                await _fire_after_provider_response(
                    hook_manager,
                    ctx=ctx,
                    inner_turn=turn_index,
                    assistant_text=assistant_text,
                    thinking_text=thinking_text,
                    tool_requests=[
                        {"call_id": tc.call_id, "name": tc.name, "args": dict(tc.args)}
                        for tc in pending.values()
                    ],
                    usage={
                        "input_tokens": llm_input,
                        "output_tokens": llm_output,
                        "cached_tokens": llm_cached,
                        "cache_read_tokens": llm_cache_read,
                        "cache_creation_tokens": llm_cache_creation,
                    },
                )
            except asyncio.CancelledError:
                for aev in stream_mapper.finish():
                    yield aev
                await _fire_after_provider_response(
                    hook_manager,
                    ctx=ctx,
                    inner_turn=turn_index,
                    provider_error="Request cancelled",
                )
                yield Error(request_id=ctx.request_id, error="Request cancelled")
                needs_followup_after_tools = False
                return
            except Exception as exc:
                for aev in stream_mapper.finish():
                    yield aev
                logger.exception(
                    "provider stream failed %s",
                    kv(request_id=ctx.request_id, thread_id=ctx.thread_id, model=ctx.model),
                )
                await _fire_after_provider_response(
                    hook_manager,
                    ctx=ctx,
                    inner_turn=turn_index,
                    provider_error=str(exc),
                )
                yield Error(request_id=ctx.request_id, error=str(exc))
                needs_followup_after_tools = False
                return

            if cancelled is not None and cancelled.is_set():
                yield Error(request_id=ctx.request_id, error="Request cancelled")
                needs_followup_after_tools = False
                break

            if not pending:
                cleaned_text = (assistant_text or "").strip()
                if cleaned_text:
                    # Fire-and-forget: this is the final assistant turn (we break
                    # right after), so nothing reads it again in-loop. Keeping it
                    # off the await path means the Firestore write does not delay
                    # TurnComplete / the user-visible reply. Awaited at the tail.
                    assistant_write_task = asyncio.create_task(
                        history.append(
                            ctx.thread_id,
                            Message(role="assistant", content=[Text(text=cleaned_text)]),
                        )
                    )
                    last_assistant[0] = cleaned_text
                    turn_output_text = cleaned_text
                    needs_followup_after_tools = False
                    break
                # Model returned no text after tool results (or only whitespace). Without another
                # provider round the user sees tools then silence — retry until turn budget.
                rows = await _load_agent_chat_history(history, ctx.thread_id)
                owes_tool_followup = needs_followup_after_tools or (
                    bool(rows)
                    and rows[-1].role == "user"
                    and any(isinstance(b, ToolResponse) for b in rows[-1].content)
                )
                if owes_tool_followup and turn_index < effective_max:
                    continue
                needs_followup_after_tools = False
                break

            ordered = sorted(pending.values(), key=lambda c: c.call_id)
            assist_blocks: list[ContentBlock] = []
            if thinking_text.strip():
                assist_blocks.append(
                    ThinkingBlock(thinking=thinking_text, signature=thinking_signature or "")
                )
            prose = (assistant_text or "").strip()
            if prose:
                assist_blocks.append(Text(text=prose))
            for c in ordered:
                assist_blocks.append(
                    ToolRequest(
                        id=c.call_id,
                        name=c.name,
                        args=dict(c.args),
                        parse_error=c.parse_error,
                        metadata=dict(c.metadata) if c.metadata else None,
                    )
                )
            await history.append(
                ctx.thread_id,
                Message(role="assistant", content=assist_blocks),
            )

            needs_followup_after_tools = True

            # One user Message for the whole model tool-call turn (all chunks).
            # Gemini 400s when functionResponse count != functionCall count on the
            # preceding model turn; serial non-task tools are separate chunks but
            # must still share a single user row.
            all_tool_responses: list[ContentBlock] = []
            mcp_registry_mutated = False
            # Computed once per batch and reused for both chunking and dispatch
            # so the two stay consistent even if ctx.tools were ever mutated
            # mid-batch (e.g. by an MCP registry reload).
            safe_names = _parallel_safe_names(ctx.tools)
            reject_batch = _should_reject_tool_batch(ordered, truncated=stream_truncated)
            if reject_batch:
                logger.warning(
                    "rejecting truncated/incomplete tool batch %s",
                    kv(
                        request_id=ctx.request_id,
                        thread_id=ctx.thread_id,
                        truncated=stream_truncated,
                        n_calls=len(ordered),
                    ),
                )
                for call in ordered:
                    err = _rejected_tool_batch_error(call, truncated=stream_truncated)
                    yield ToolCallStarted(
                        request_id=ctx.request_id,
                        tool=call.name,
                        label=call.name,
                        args=dict(call.args),
                        parse_error=call.parse_error,
                    )
                    result_evt, tool_resp = _finish_tool(
                        call, ToolExecutionResult.err(err)
                    )
                    yield result_evt
                    all_tool_responses.append(tool_resp)

            for chunk in (() if reject_batch else _chunk_tool_calls(
                ordered, parallel_safe=safe_names
            )):
                if cancelled is not None and cancelled.is_set():
                    yield Error(request_id=ctx.request_id, error="Request cancelled")
                    needs_followup_after_tools = False
                    return

                allowed_exec: list[ToolCall] = []
                # Collect ToolResponse blocks for this chunk; merged into
                # ``all_tool_responses`` after every chunk completes.
                chunk_responses: list[ContentBlock] = []

                for call in chunk:
                    if cancelled is not None and cancelled.is_set():
                        yield Error(request_id=ctx.request_id, error="Request cancelled")
                        needs_followup_after_tools = False
                        return

                    # A provider couldn't parse the streamed tool JSON: args are
                    # empty and ``parse_error`` holds the reason. Surface it as a
                    # tool error result so the model can self-correct instead of
                    # executing the tool with empty arguments.
                    if call.parse_error:
                        yield ToolCallStarted(
                            request_id=ctx.request_id,
                            tool=call.name,
                            label=call.name,
                            args=dict(call.args),
                            parse_error=call.parse_error,
                            call_id=call.call_id,
                        )
                        result_evt, tool_resp = _finish_tool(
                            call, ToolExecutionResult.err(call.parse_error)
                        )
                        yield result_evt
                        chunk_responses.append(tool_resp)
                        continue

                    inspector_call = InspectorToolCall(
                        call_id=call.call_id, name=call.name, args=dict(call.args)
                    )
                    allowed = True
                    denial_message: str | None = None
                    for insp in inspectors:
                        decision = await insp.check(inspector_call, ctx)
                        match decision.kind:
                            case "allow":
                                pass
                            case "deny":
                                allowed = False
                                denial_message = decision.message
                                logger.debug(
                                    "tool inspector deny %s",
                                    kv(
                                        request_id=ctx.request_id,
                                        thread_id=ctx.thread_id,
                                        tool=call.name,
                                        call_id=call.call_id,
                                        decision="deny",
                                    ),
                                )
                                break
                            case "confirm":
                                if ctx.sse_bus is None:
                                    allowed = False
                                    denial_message = (
                                        decision.message
                                        or "Confirmation required but no SSE session is available"
                                    )
                                    break
                                bus = ctx.sse_bus
                                fut = bus.register_pending(call.call_id)
                                yield ToolConfirmationRequestEvent(
                                    request_id=ctx.request_id,
                                    tool_call_id=call.call_id,
                                    tool_name=call.name,
                                    arguments=dict(call.args),
                                    prompt=decision.message,
                                )
                                try:
                                    payload = await _await_user_response_any(
                                        bus, fut, call.call_id, timeout_sec=None
                                    )
                                except asyncio.CancelledError:
                                    # Re-raise so turn cancellation (client disconnect /
                                    # abort) propagates; do not continue the tool loop
                                    # on a dead session.
                                    raise
                                if payload.get("_timeout"):
                                    allowed = False
                                    to = int(
                                        float(os.environ.get("PENDING_RESPONSE_TIMEOUT_SEC", "300"))
                                    )
                                    denial_message = f"user did not respond within {to}s"
                                    break
                                if payload.get("approved"):
                                    allowed = True
                                    if payload.get("always"):
                                        remember_always_approval(
                                            bus, call.name, resource_for_call(inspector_call)
                                        )
                                        logger.debug(
                                            "tool inspector confirm %s",
                                            kv(
                                                request_id=ctx.request_id,
                                                thread_id=ctx.thread_id,
                                                tool=call.name,
                                                call_id=call.call_id,
                                                decision="confirm_always",
                                            ),
                                        )
                                    logger.debug(
                                        "tool inspector confirm %s",
                                        kv(
                                            request_id=ctx.request_id,
                                            thread_id=ctx.thread_id,
                                            tool=call.name,
                                            call_id=call.call_id,
                                            decision="confirm_approved",
                                        ),
                                    )
                                else:
                                    allowed = False
                                    reason_raw = payload.get("reason")
                                    denial_message = (
                                        (reason_raw if isinstance(reason_raw, str) else None)
                                        or "denied by user"
                                    )
                                    logger.debug(
                                        "tool inspector confirm %s",
                                        kv(
                                            request_id=ctx.request_id,
                                            thread_id=ctx.thread_id,
                                            tool=call.name,
                                            call_id=call.call_id,
                                            decision="confirm_denied",
                                        ),
                                    )
                                break

                    if not allowed:
                        msg = denial_message or "tool call denied"
                        yield ToolCallStarted(
                            request_id=ctx.request_id,
                            tool=call.name,
                            label=call.name,
                            args=dict(call.args),
                            call_id=call.call_id,
                        )
                        result_evt, tool_resp = _finish_tool(
                            call, ToolExecutionResult.err(msg)
                        )
                        yield result_evt
                        chunk_responses.append(tool_resp)
                        continue

                    yield ToolCallStarted(
                        request_id=ctx.request_id,
                        tool=call.name,
                        label=call.name,
                        args=dict(call.args),
                        call_id=call.call_id,
                    )
                    allowed_exec.append(call)

                if not allowed_exec:
                    all_tool_responses.extend(chunk_responses)
                    continue

                # ToolCallStarted already published via async-gen consumer;
                # PRE_TOOL is awaited inside _execute_one_tool_call.
                parallel_chunk = (
                    all(c.name == "task" for c in allowed_exec)
                    or (
                        len(allowed_exec) > 1
                        and all(c.name in safe_names for c in allowed_exec)
                    )
                )
                dispatch_mode = "parallel" if parallel_chunk else "serial"
                logger.debug(
                    "tool dispatch %s",
                    kv(
                        request_id=ctx.request_id,
                        thread_id=ctx.thread_id,
                        turn=turn_index,
                        mode=dispatch_mode,
                        chunk_size=len(allowed_exec),
                    ),
                )

                async def _execute_one_tool_call(
                    call: ToolCall,
                    *,
                    _ctx: TurnContext = ctx,
                ) -> ToolExecutionResult:
                    nonlocal pre_tool_extra_next
                    logger.debug(
                        "tool execute %s",
                        kv(
                            request_id=_ctx.request_id,
                            thread_id=_ctx.thread_id,
                            tool=call.name,
                            call_id=call.call_id,
                        ),
                    )
                    async with span_tool(
                        tool_name=call.name,
                        tool_call_id=call.call_id,
                        thread_id=_ctx.thread_id,
                        request_id=_ctx.request_id,
                        args=dict(call.args),
                    ):
                        pre_tool_payload = await _fire_hook(
                            hook_manager,
                            event=HookEvent.PRE_TOOL,
                            ctx=_ctx,
                            timeout_s=_HOOK_PRE_TOOL_TIMEOUT_S,
                            tool_name=call.name,
                            tool_args=dict(call.args),
                        )
                        if pre_tool_payload is not None and pre_tool_payload.inject_text:
                            pre_tool_extra_next = _combine_extras(
                                pre_tool_extra_next, pre_tool_payload.inject_text
                            )
                        _record_tool_hook_span_event("pre_tool", call.name)
                        try:
                            tool_result = await tool_executor.execute(call=call, ctx=_ctx)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.warning(
                                "tool execution failed %s",
                                kv(
                                    request_id=_ctx.request_id,
                                    thread_id=_ctx.thread_id,
                                    tool=call.name,
                                    call_id=call.call_id,
                                ),
                                exc_info=True,
                            )
                            tool_result = ToolExecutionResult.err(str(exc))
                        result_summary = (
                            _blocks_to_sse_summary(tool_result.blocks)
                            if tool_result.error is None
                            else None
                        )
                        record_tool_outcome(result_summary, tool_result.error)
                        await _fire_hook(
                            hook_manager,
                            event=HookEvent.POST_TOOL,
                            ctx=_ctx,
                            timeout_s=0,
                            tool_name=call.name,
                            tool_args=dict(call.args),
                            tool_result=result_summary,
                            tool_error=tool_result.error,
                        )
                        _record_tool_hook_span_event("post_tool", call.name)
                    return tool_result

                if not parallel_chunk:
                    call = allowed_exec[0]
                    try:
                        tool_result = await _execute_one_tool_call(call)
                    except asyncio.CancelledError:
                        yield Error(request_id=ctx.request_id, error="Request cancelled")
                        needs_followup_after_tools = False
                        return

                    event, response = _finish_tool(call, tool_result)
                    yield event
                    for img_evt in _image_events(ctx.request_id, call.call_id, tool_result):
                        yield img_evt
                    chunk_responses.append(response)
                    if (
                        tool_result.error is None
                        and call.name in MCP_REGISTRY_MUTATING_TOOLS
                    ):
                        mcp_registry_mutated = True
                else:
                    if cancelled is not None and cancelled.is_set():
                        yield Error(request_id=ctx.request_id, error="Request cancelled")
                        needs_followup_after_tools = False
                        return

                    conc = (
                        _MAX_CONCURRENT_SUBAGENTS
                        if all(c.name == "task" for c in allowed_exec)
                        else _parallel_tool_concurrency()
                    )
                    sem = asyncio.Semaphore(conc)

                    async def _run_parallel(
                        call: ToolCall,
                        *,
                        _sem: asyncio.Semaphore = sem,
                        _ctx: TurnContext = ctx,
                    ) -> tuple[ToolCall, ToolExecutionResult]:
                        async with _sem:
                            return (call, await _execute_one_tool_call(call, _ctx=_ctx))

                    try:
                        outcomes = await asyncio.gather(
                            *[_run_parallel(c) for c in allowed_exec],
                            return_exceptions=True,
                        )
                    except asyncio.CancelledError:
                        yield Error(request_id=ctx.request_id, error="Request cancelled")
                        needs_followup_after_tools = False
                        return

                    for idx, outcome in enumerate(outcomes):
                        call = allowed_exec[idx]
                        if isinstance(outcome, asyncio.CancelledError):
                            yield Error(request_id=ctx.request_id, error="Request cancelled")
                            needs_followup_after_tools = False
                            return
                        if isinstance(outcome, Exception):
                            logger.warning(
                                "tool execution failed %s",
                                kv(
                                    request_id=ctx.request_id,
                                    thread_id=ctx.thread_id,
                                    tool=call.name,
                                    call_id=call.call_id,
                                ),
                                exc_info=(type(outcome), outcome, outcome.__traceback__),
                            )
                            tool_result = ToolExecutionResult.err(str(outcome))
                        else:
                            _, tool_result = cast(tuple[ToolCall, ToolExecutionResult], outcome)

                        event, response = _finish_tool(call, tool_result)
                        yield event
                        for img_evt in _image_events(ctx.request_id, call.call_id, tool_result):
                            yield img_evt
                        chunk_responses.append(response)
                        if (
                            tool_result.error is None
                            and call.name in MCP_REGISTRY_MUTATING_TOOLS
                        ):
                            mcp_registry_mutated = True

                all_tool_responses.extend(chunk_responses)
            doom_msg = doom_tracker.take_error()
            if doom_msg is not None:
                logger.warning(
                    "doom loop detected %s",
                    kv(
                        request_id=ctx.request_id,
                        thread_id=ctx.thread_id,
                        tool=doom_tracker.triggered_tool,
                        threshold=doom_tracker.threshold,
                    ),
                )
                yield Error(request_id=ctx.request_id, error=doom_msg)

            if all_tool_responses:
                chat_for_budget = await _load_agent_chat_history(history, ctx.thread_id)
                budget_used = usage.estimated_prompt_tokens
                try:
                    budget_used = await _prompt_input_tokens_for_history(
                        ctx=ctx,
                        chat_messages=chat_for_budget,
                        provider=provider,
                        attachment_store=attachment_store,
                        attachment_catalog=attachment_catalog,
                        memory_selection=memory_selection,
                        extra_system_text=pre_turn_extra,
                        vertex_google_search=vertex_google_search,
                        epoch=epoch_tracker,
                    )
                except Exception:
                    logger.warning(
                        "tool budget token recount failed %s; using estimated_prompt_tokens",
                        kv(request_id=ctx.request_id, thread_id=ctx.thread_id),
                        exc_info=True,
                    )
                usage.estimated_prompt_tokens = max(
                    usage.estimated_prompt_tokens, budget_used
                )
                budgeter = ContextBudgeter.from_env(
                    window_tokens=ctx.context_window_tokens,
                    used_tokens=budget_used,
                )
                async for budget_evt in _append_budgeted_tool_responses(
                    chunk_responses=all_tool_responses,
                    ctx=ctx,
                    history=history,
                    usage=usage,
                    budgeter=budgeter,
                    provider=provider,
                    vertex_google_search=vertex_google_search,
                    epoch=epoch_tracker,
                ):
                    yield budget_evt

            if mcp_registry_mutated:
                mcp_client = getattr(tool_executor, "mcp", None)
                if mcp_client is None:
                    logger.error(
                        "MCP registry mutated but tool executor has no mcp client %s",
                        kv(request_id=ctx.request_id, thread_id=ctx.thread_id),
                    )
                    raise RuntimeError(
                        "tool executor missing mcp client after MCP registry mutation"
                    )
                ctx = refresh_tools_after_mcp_change(ctx, mcp_client)
                tools_dirty = True
                doom_tracker.exempt_names = _doom_loop_exempt_names(ctx.tools)
                logger.info(
                    "refreshed ctx.tools after MCP registry change %s",
                    kv(
                        request_id=ctx.request_id,
                        thread_id=ctx.thread_id,
                        tool_count=len(ctx.tools),
                    ),
                )

        finally:
            set_turn_prompt_tokens(usage.estimated_prompt_tokens)
            if turn_output_text:
                set_turn_io(output_value=turn_output_text)
            end_turn_span(turn_span)
    if needs_followup_after_tools:
        logger.warning(
            "max turns exceeded %s",
            kv(
                request_id=ctx.request_id,
                thread_id=ctx.thread_id,
                max_turns=effective_max,
            ),
        )
        yield Error(request_id=ctx.request_id, error="Max turns exceeded")

    # Ensure the backgrounded assistant write has landed before any load/reset
    # below (freeze) so the assistant row is durable and not overwritten.
    await _await_history_write(assistant_write_task)

    descriptor_events = await freeze_attachments_in_history(
        thread_id=ctx.thread_id,
        history=history,
        catalog=attachment_catalog,
        last_assistant_text=last_assistant[0],
    )
    for desc_evt in descriptor_events:
        yield AttachmentDescriptorEvent(
            request_id=ctx.request_id,
            attachment_id=desc_evt.attachment_id,
            mime_type=desc_evt.mime_type,
            filename=desc_evt.filename,
            description=desc_evt.description,
        )

    await _fire_hook(
        hook_manager,
        event=HookEvent.POST_TURN,
        ctx=ctx,
        timeout_s=0,
    )
    # Settlement for POST_TURN / lingering POST_TOOL runs in run() finally
    # before TurnComplete — do not drain twice here.
