"""Inner turn loop: prepare → compact → stream → empty/final or tool dispatch."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import aclosing
from typing import Any, Literal, cast

from monkeybot.core.attachments.catalog import SessionAttachmentCatalog
from monkeybot.core.attachments.freeze import freeze_attachments_in_history
from monkeybot.core.attachments.store import AttachmentStore
from monkeybot.core.context import (
    TurnContext,
    refresh_memory_index,
)
from monkeybot.core.context.epoch import ContextEpochTracker, EpochAdmit
from monkeybot.core.context.project_context import load_project_instructions
from monkeybot.core.hooks import HookEvent, HookManager
from monkeybot.core.llm.context_overflow import is_context_overflow
from monkeybot.core.llm.provider import (
    Done,
    Message,
    Provider,
    ToolCall,
    UsageEvent,
    provider_stream,
)
from monkeybot.core.llm.usage import Usage
from monkeybot.core.logging_utils import kv
from monkeybot.core.mcp.session_visibility import advertised_servers, servers_used_by_calls
from monkeybot.core.memory.ingest import persist_message
from monkeybot.core.messages import convert_to_provider
from monkeybot.core.messages.tool_integrity import (
    cancelled_tool_result_text,
    interrupted_tool_result_text,
    preempted_tool_result_text,
)
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.persistence.row_ids import new_row_id
from monkeybot.core.persistence.transcript import TranscriptWriter
from monkeybot.core.prompts.prompt import latest_user_message_text
from monkeybot.core.runtime.context_budget import (
    SUMMARY_TRIGGER_RATIO,
    compute_context_pressure_tier,
)
from monkeybot.core.runtime.provider_stream_mapper import ProviderStreamMapper
from monkeybot.core.tools.background_jobs import (
    jobs_check_failed_error,
    jobs_still_running_note,
    jobs_will_be_killed_error,
)
from monkeybot.core.tools.inspector import ToolInspector
from monkeybot.core.types.content_blocks import (
    ContentBlock,
    SystemNotification,
    Text,
    ToolRequest,
    ToolResponse,
)
from monkeybot.core.types.content_blocks import Thinking as ThinkingBlock
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.core.verifier.intervention import correction_text
from monkeybot.core.verifier.mailbox import VerdictMailbox
from monkeybot.core.verifier.severity import cap_severity
from monkeybot.providers._utils import note_anthropic_token_estimate_observation
from monkeybot.providers.pricing import estimate_cost

from .background_summary import maybe_start
from .doom_loop import (
    _doom_loop_exempt_names,
    _DoomLoopTracker,
    _effective_doom_loop_threshold,
)
from .events import (
    AgentEvent,
    AssistantDelta,
    AssistantTextEnded,
    AssistantTextStarted,
    AttachmentDescriptorEvent,
    ContextSummarized,
    ContextSummarizing,
    ContextUsage,
    Error,
    HarnessIntervention,
    SystemNotificationEvent,
    SystemPromptSnapshot,
    Thinking,
    ThinkingBlockComplete,
    ToolCallResult,
    UserSteered,
    VerifierVerdict,
)
from .history_compaction import (
    HISTORY_LOAD_MAX,
    _await_history_write,
    _compact_history_if_needed,
    _fire_history_compacted,
    _intent_facts_from_ctx,
    _summarization_viable,
    _workspace_trailer_from_ctx,
    bind_summary_transcript,
    protect_recent_count,
    reset_summary_transcript,
    truncate_history_preserving_pins,
)
from .input_admission import InputAdmission, SteerItem, join_text, preview_text
from .loop_hooks import (
    _HOOK_READ_TIMEOUT_S,
    _apply_before_provider_hook,
    _drain_hook_settlement,
    _fire_after_provider_response,
    _fire_hook,
    _scan_and_redact_outbound,
)
from .loop_messages import (
    _admit_system_context,
    _append_extra_system_text,
    _combine_extras,
    _epoch_events,
    _load_agent_chat_history,
    _messages_for_provider,
    _provider_messages_prompt_summary,
    _snapshot_system_message,
    _system_message_from_text,
    _system_prompt_snapshot_text,
    _user_text_from_content,
)
from .loop_ports import ToolExecutorPort
from .loop_usage import (
    _effective_max_turns,
    _merge_usage_event,
    _provider_call_hints,
    _provider_prompt_input_tokens,
    _stream_thinking_budget,
)
from .preempt import StepPreemptedError, StreamStalledError, race_preempt
from .provider_retry import (
    RESUME_NOTE,
    RESUME_NOTICE,
    classify,
    idle_timeout_s,
    policy_for,
    recovery,
    retry_notice,
)
from .tool_dispatch import ToolBatchState, dispatch_tool_batch

logger = logging.getLogger("monkeybot.core.runtime.loop.turn_loop")

# Emitted as an ``Error`` event when a run hits its turn budget. The parent task
# tool matches on this exact value to report ``exit_reason="max_turns"``, so it is
# a contract between the loop and the subagent completion payload, not a message.
MAX_TURNS_ERROR = "Max turns exceeded"
CONTEXT_OVERFLOW_ERROR = (
    "This request is too large for the model's context window. Send it in smaller pieces."
)

TurnAction = Literal["continue", "break", "return", "preempt", "resume", "overflow"]

_EMPTY_COMPLETION_RETRIES = 2
_POST_TOOL_EMPTY_RETRIES = 1
_EMPTY_COMPLETION_RECOVERY_NOTE = (
    "[Harness] Empty completion: you produced no assistant text and no tool "
    "calls. Either invoke a tool through the native function-call channel, or "
    "give the user a short natural-language answer. Do not stay silent."
)
_POST_TOOL_EMPTY_COMPLETION_NOTE = (
    "[Harness] Post-tool empty completion: tool results are already in history. "
    "Answer from those results now. Do not re-run tools unless a concrete gap "
    "remains, and do not stay silent."
)
_EMPTY_COMPLETION_EXHAUSTED_ERROR = (
    "Empty model completion after recovery: ending turn with no reply"
)
_BACKGROUND_JOB_NUDGES = 3
# Upper bound on PRE_FINAL_ANSWER refusals per user message; hooks apply their own, lower cap.
_FINAL_ANSWER_REJECTIONS = 3


def _require_prompt(state: _TurnState) -> tuple[Message, EpochAdmit]:
    if state.system is None or state.admit is None:
        raise RuntimeError("turn prepare did not set system/admit")
    return state.system, state.admit


def _rebind_summary_transcript(
    holder: list[Any] | None,
    writer: TranscriptWriter | None,
    *,
    request_id: str,
    inner_turn: int,
) -> None:
    """Point summary recording at this inner turn. A background job keeps the previous binding."""
    if holder is None:
        return
    current = holder[0]
    if current is not None:
        reset_summary_transcript(current)
        holder[0] = None
    if writer is None:
        return
    holder[0] = bind_summary_transcript(writer, request_id=request_id, inner_turn=inner_turn)


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
    trace_id_out: list[str | None] | None = None,
    attachment_store: AttachmentStore | None = None,
    attachment_catalog: SessionAttachmentCatalog | None = None,
    transcript_writer: TranscriptWriter | None = None,
    vertex_google_search: bool = False,
    input_admission: InputAdmission | None = None,
    input_provenance: str = "human",
) -> AsyncIterator[AgentEvent]:
    from monkeybot.observability.spans import set_run_output, span_run

    last_assistant: list[str] = [""]
    user_text = _user_text_from_content(user_content)
    summary_token: list[Any] = [None]
    async with span_run(ctx, user_message=user_text):
        try:
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
                last_assistant=last_assistant,
                attachment_store=attachment_store,
                attachment_catalog=attachment_catalog,
                transcript_writer=transcript_writer,
                vertex_google_search=vertex_google_search,
                input_admission=input_admission,
                input_provenance=input_provenance,
                summary_token=summary_token,
            ):
                yield evt
        finally:
            current = summary_token[0]
            if current is not None:
                reset_summary_transcript(current)
                summary_token[0] = None
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
                logger.debug(
                    "observability trace id unavailable %s",
                    kv(request_id=ctx.request_id, thread_id=ctx.thread_id),
                    exc_info=True,
                )


def _verdict_grace_s(ctx: TurnContext) -> float:
    """Seconds a drain may wait on an in-flight judge. Production ships at 0."""
    if ctx.config is None:
        return 0.0
    try:
        return max(0.0, float(ctx.config.verifier.judge.tail_grace_s))
    except (AttributeError, TypeError, ValueError):
        return 0.0


async def _drain_steers(
    *,
    input_admission: InputAdmission | None,
    history: HistoryStore,
    ctx: TurnContext,
) -> AsyncIterator[AgentEvent]:
    """Inject queued steer messages at a safe boundary (before next provider call)."""
    if input_admission is not None:
        while True:
            item = input_admission.pop_steer()
            if item is None:
                break
            steered = item.content
            row_id = new_row_id()
            await persist_message(
                history,
                Message(role="user", content=list(steered), row_id=row_id),
                thread_id=ctx.thread_id,
                turn_id=ctx.request_id,
                memory=ctx.memory,
                ingest=True,
            )
            preview = preview_text(steered)
            _admit_steer_to_ledger(ctx, join_text(steered), item, row_id)
            logger.info(
                "steer injected %s",
                kv(
                    request_id=ctx.request_id,
                    thread_id=ctx.thread_id,
                    preview=preview[:80],
                    provenance=item.provenance,
                ),
            )
            yield UserSteered(
                request_id=ctx.request_id,
                text=preview,
                queued_request_id=item.queued_request_id or "",
            )
        # The preempt that woke this step must not abort the provider call
        # that is about to see the injected text.
        input_admission.clear_preempt()
    async for verdict_evt in _drain_verdicts(ctx, history):
        yield verdict_evt


async def _take_ready(
    mailbox: VerdictMailbox, thread_id: str, request_id: str, *, grace_s: float
) -> list[VerifierVerdict]:
    """Pop ready verdicts for this request.

    Waits out ``grace_s`` only while a request-specific judge call is still in
    flight. Fast failures (pending drops to zero) return immediately instead of
    paying the full deadline.
    """
    collected = mailbox.take_ready(thread_id, request_id)
    if grace_s <= 0 or not mailbox.pending(thread_id, request_id):
        return collected
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace_s
    while mailbox.pending(thread_id, request_id) and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))
        collected.extend(mailbox.take_ready(thread_id, request_id))
    if mailbox.pending(thread_id, request_id):
        logger.info(
            "verdict tail stale %s",
            kv(thread_id=thread_id, request_id=request_id),
        )
    return collected


async def _drain_verdicts(
    ctx: TurnContext,
    history: HistoryStore | None = None,
    *,
    grace_s: float = 0.0,
) -> AsyncIterator[AgentEvent]:
    """Commit ready verdicts at a safe boundary. Never waits on a judge unless ``grace_s``."""
    mailbox = ctx.verdict_mailbox
    if mailbox is None:
        return
    try:
        ready = await _take_ready(mailbox, ctx.thread_id, ctx.request_id, grace_s=grace_s)
    except Exception:
        logger.warning(
            "verdict mailbox drain failed %s",
            kv(thread_id=ctx.thread_id, request_id=ctx.request_id),
            exc_info=True,
        )
        return
    max_sev = "nudge"
    if ctx.config is not None:
        max_sev = ctx.config.verifier.escalation.max_severity

    for verdict in ready:
        if not isinstance(verdict, VerifierVerdict):
            logger.warning(
                "verdict drain skipped non-verdict %s",
                kv(thread_id=ctx.thread_id, type=type(verdict).__name__),
            )
            continue
        requested = verdict.severity
        capped = cap_severity(requested, max_sev)
        if capped != requested:
            logger.info(
                "verdict severity capped %s",
                kv(
                    thread_id=ctx.thread_id,
                    requested=requested,
                    capped=capped,
                    maximum=max_sev,
                ),
            )
            verdict = dataclasses.replace(verdict, severity=capped)
        trusted = (
            None
            if capped in ("none", "") or verdict.status == "on_track"
            else correction_text(verdict.triggering_signals)
        )
        if trusted != verdict.correction:
            verdict = dataclasses.replace(verdict, correction=trusted)
        if history is not None:
            try:
                await persist_message(
                    history,
                    Message(
                        role="system",
                        content=[
                            SystemNotification(
                                notification_type="verifierVerdict",
                                msg=verdict.rationale,
                                data=verdict.to_wire(),
                            )
                        ],
                    ),
                    thread_id=ctx.thread_id,
                    turn_id=ctx.request_id,
                    memory=ctx.memory,
                    ingest=False,
                )
            except Exception:
                logger.warning(
                    "verdict persist failed %s",
                    kv(thread_id=ctx.thread_id, request_id=ctx.request_id),
                    exc_info=True,
                )
        _stash_escalation(mailbox, ctx.thread_id, capped, verdict)
        yield verdict


def _stash_escalation(
    mailbox: VerdictMailbox, thread_id: str, capped: str, verdict: VerifierVerdict
) -> None:
    """Arm a sticky nudge or one-shot replan for this verdict's own request."""
    mailbox.arm_escalation(thread_id, verdict, max_severity=capped)


def _write_nudge_unresolved(state: _TurnState) -> bool:
    """True when this request still owes a read for a write-without-read nudge."""
    mailbox = state.ctx.verdict_mailbox
    if mailbox is None:
        return False
    return "write_without_read" in mailbox.active_nudge_signals(
        state.ctx.thread_id, state.ctx.request_id
    )


def _arm_replan_from_mailbox(state: _TurnState) -> str | None:
    """Reuse doom-loop ``force_no_tools`` for a drained ``replan`` verdict.

    Returns the replan note when one was armed, otherwise ``None``.
    """
    mailbox = state.ctx.verdict_mailbox
    if mailbox is None:
        return None
    try:
        note = mailbox.take_replan(state.ctx.thread_id, state.ctx.request_id)
    except Exception:
        logger.warning(
            "replan mailbox take failed %s",
            kv(thread_id=state.ctx.thread_id, request_id=state.ctx.request_id),
            exc_info=True,
        )
        return None
    if not note:
        return None
    state.verifier_replan_turn = True
    state.doom_tracker.force_no_tools = True
    existing = state.doom_tracker.recovery_note
    state.doom_tracker.recovery_note = f"{existing}\n\n{note}" if existing else note
    logger.info(
        "verifier replan armed %s",
        kv(thread_id=state.ctx.thread_id, request_id=state.ctx.request_id),
    )
    return note


def _admit_steer_to_ledger(
    ctx: TurnContext, verbatim: str, item: SteerItem, row_id: str | None = None
) -> None:
    ledger = ctx.goal_ledger
    if ledger is None or not verbatim:
        return
    from monkeybot.core.persistence.goal_ledger import Channel, Provenance

    provenance = (
        Provenance.VERIFIER_STEER
        if item.provenance == Provenance.VERIFIER_STEER.value
        else Provenance.HUMAN
    )
    try:
        ledger.admit(
            ctx.thread_id,
            verbatim,
            provenance=provenance,
            channel=Channel.STEER,
            source_row_id=row_id,
        )
    except Exception:
        logger.warning(
            "goal_ledger steer tap failed %s",
            kv(thread_id=ctx.thread_id, request_id=ctx.request_id),
            exc_info=True,
        )


def _tools_schema_hash(tools: Sequence[ToolDef]) -> str:
    payload = [t.to_model_schema() for t in tools]
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _mcp_known_servers(tool_executor: ToolExecutorPort) -> list[str]:
    """Server names the executor's MCP client has seen. Empty when there is none."""
    mcp = getattr(tool_executor, "mcp", None)
    names = getattr(mcp, "known_server_names", None)
    if not callable(names):
        return []
    return list(names())


def _note_mcp_idle_turn(state: _TurnState, tool_executor: ToolExecutorPort) -> None:
    """Count this model call against servers whose tools were offered.

    A recovery call that sent no tools (``tools_suppressed``) does not count.
    Calls the model made reset a visible server. Hiding itself waits for the
    tool-batch boundary.
    """
    visibility = state.ctx.mcp_visibility
    if visibility is None:
        return
    known = _mcp_known_servers(tool_executor)
    advertised: set[str] = set()
    if not state.tools_suppressed:
        advertised = advertised_servers(state.turn_tools, known)
    calls = [(call.name, call.args) for call in state.pending.values()]
    visibility.note_inner_turn(advertised, servers_used_by_calls(calls, known))


def _mark_tools_dirty(state: _TurnState, reason: str, tools: Sequence[ToolDef]) -> None:
    digest = _tools_schema_hash(tools)
    if digest != state.tools_schema_hash:
        state.tools_dirty = True
        # First cause wins: a BEFORE_PROVIDER hook re-marking tools that MCP already
        # dirtied must not relabel the change as its own.
        if state.tools_dirty_reason is None:
            state.tools_dirty_reason = reason
    state.tools_schema_hash = digest


@dataclasses.dataclass
class _TurnState:
    """Long-lived + per-turn working state for one user message."""

    ctx: TurnContext
    user_text: str
    effective_max: int
    doom_tracker: _DoomLoopTracker
    epoch_tracker: ContextEpochTracker = dataclasses.field(default_factory=ContextEpochTracker)
    turn_index: int = 0
    needs_followup_after_tools: bool = False
    provider_messages_written: int = 0
    # Independent of provider_messages_written: that counter only advances
    # when a transcript_writer is configured (see
    # _write_transcript_provider_request), but the egress scan must run
    # every turn regardless of whether transcripts are enabled.
    provider_messages_scanned: int = 0
    tools_dirty: bool = False
    assistant_write_task: asyncio.Task[None] | None = None
    pre_turn_extra: str | None = None
    pre_tool_extra_next: str | None = None
    # Runtime notes already folded into this inner turn's system prompt.
    # History compaction rebuilds that prompt; it must reapply these notes or
    # a one-shot PRE_TOOL reminder is dropped before the model sees it.
    turn_runtime_notes: str | None = None
    empty_completion_retries_left: int = _EMPTY_COMPLETION_RETRIES
    post_tool_empty_retries_left: int = _POST_TOOL_EMPTY_RETRIES
    background_job_nudges_left: int = _BACKGROUND_JOB_NUDGES
    background_jobs_reported: bool = False
    # While background jobs run, a text-only reply may be blocked by the nudge,
    # so assistant text events are held back until the turn knows it may finish.
    hold_assistant_text: bool = False
    # Set by BEFORE_PROVIDER_REQUEST hooks for the next stream (see ``HookPayload``).
    hold_for_hooks: bool = False
    final_answer_rejections_left: int = _FINAL_ANSWER_REJECTIONS
    held_text_events: list[AgentEvent] = dataclasses.field(default_factory=list)
    turn_input_text: str = ""
    turn_output_text: str = ""
    chat_messages: list[Message] = dataclasses.field(default_factory=list)
    system: Message | None = None
    admit: EpochAdmit | None = None
    turn_tools: Sequence[ToolDef] = ()
    provider_messages: list[Message] = dataclasses.field(default_factory=list)
    resolved_messages: list[Message] = dataclasses.field(default_factory=list)
    stream_thinking: int | None = None
    action: TurnAction | None = None
    # This inner turn consumed a verifier replan, so a text reply is the plan
    # restatement and the loop calls the model once more with tools restored.
    verifier_replan_turn: bool = False
    # Doom-loop and replan recovery empty the tool list for this inner turn.
    tools_suppressed: bool = False
    pending: dict[str, ToolCall] = dataclasses.field(default_factory=dict)
    assistant_text: str = ""
    thinking_text: str = ""
    thinking_signature: str | None = None
    stream_truncated: bool = False
    last_preflight_tokens: int = 0
    provider_stream_failed: bool = False
    # Resumes keep text the client already saw. Capped for the whole user request.
    provider_resumes_used: int = 0
    # One size rejection per send is answered by compacting and sending again.
    overflow_recovered: bool = False
    tools_dirty_reason: str | None = None
    tools_schema_hash: str = ""


async def _reload_project_instructions(ctx: TurnContext) -> TurnContext:
    """Re-read AGENTS.md so a change between inner turns opens a new epoch.

    Instructions are part of the stable prefix. The file can change while a
    reply is still running; the next provider call has to see the new text.
    """
    config = ctx.config
    root = ctx.workspace_root
    if config is None or root is None:
        return ctx
    names = config.coding.project_instructions_files
    instructions = await asyncio.to_thread(load_project_instructions, root, names)
    if instructions == ctx.project_instructions:
        return ctx
    return dataclasses.replace(ctx, project_instructions=instructions)


async def _prepare_turn_context(
    state: _TurnState,
    *,
    history: HistoryStore,
    hook_manager: HookManager | None,
    attachment_store: AttachmentStore | None,
    attachment_catalog: SessionAttachmentCatalog | None,
) -> AsyncIterator[AgentEvent]:
    """Load history, memory, epoch admit, system + tool-definition hooks."""
    from monkeybot.observability.spans import set_turn_io

    state.chat_messages = await _load_agent_chat_history(history, state.ctx.thread_id)
    state.ctx = await refresh_memory_index(state.ctx)
    state.ctx = await _reload_project_instructions(state.ctx)
    state.turn_input_text = latest_user_message_text(state.chat_messages) or state.user_text
    set_turn_io(input_value=state.turn_input_text)

    if state.turn_index == 1:
        pre_turn_payload = await _fire_hook(
            hook_manager,
            event=HookEvent.PRE_TURN,
            ctx=state.ctx,
            timeout_s=_HOOK_READ_TIMEOUT_S,
            user_message=state.user_text,
        )
        if pre_turn_payload is not None:
            state.pre_turn_extra = pre_turn_payload.inject_text
            if pre_turn_payload.inject_memory_lines:
                state.ctx = dataclasses.replace(
                    state.ctx,
                    memory_index=[
                        *state.ctx.memory_index,
                        *pre_turn_payload.inject_memory_lines,
                    ],
                )

    state.admit = _admit_system_context(
        state.epoch_tracker,
        state.ctx,
        state.chat_messages,
        attachment_catalog=attachment_catalog,
    )
    for epoch_evt in _epoch_events(
        state.admit, request_id=state.ctx.request_id, thread_id=state.ctx.thread_id
    ):
        yield epoch_evt
    system = _system_message_from_text(state.admit.leading_system_text)
    combined_extra = _combine_extras(state.pre_turn_extra, state.pre_tool_extra_next)
    replan_note = _arm_replan_from_mailbox(state)
    if replan_note:
        yield HarnessIntervention(
            request_id=state.ctx.request_id,
            intervention="verifier_replan",
            inner_turn=state.turn_index,
            detail=replan_note,
        )
    force_no_tools, doom_loop_note = state.doom_tracker.consume_recovery()
    state.tools_suppressed = force_no_tools
    combined_extra = _combine_extras(combined_extra, doom_loop_note)
    state.turn_runtime_notes = combined_extra
    state.system = _append_extra_system_text(system, combined_extra)
    state.pre_tool_extra_next = None
    state.turn_tools = () if force_no_tools else state.ctx.tools
    tool_def_payload = await _fire_hook(
        hook_manager,
        event=HookEvent.TOOL_DEFINITION,
        ctx=state.ctx,
        timeout_s=_HOOK_READ_TIMEOUT_S,
        tools=list(state.turn_tools),
        inner_turn=state.turn_index,
    )
    if tool_def_payload is not None and tool_def_payload.tools is not None:
        state.turn_tools = list(tool_def_payload.tools)
        _mark_tools_dirty(state, "hook", state.turn_tools)
        logger.debug(
            "tool.definition hook replaced tools %s",
            kv(
                request_id=state.ctx.request_id,
                thread_id=state.ctx.thread_id,
                turn=state.turn_index,
                tool_count=len(state.turn_tools),
            ),
        )
    elif not state.tools_schema_hash:
        state.tools_schema_hash = _tools_schema_hash(state.turn_tools)
    # Preflight uses unshaped history; pressure shaping applied after token count.
    state.resolved_messages = convert_to_provider(
        state.chat_messages,
        attachment_store=attachment_store,
        session_id=state.ctx.session_id,
    )
    system_msg, admit = _require_prompt(state)
    state.provider_messages = _messages_for_provider(
        system_msg,
        state.resolved_messages,
        mid_conversation_update=admit.mid_conversation_update,
    )


async def _refresh_prompt_after_history_change(
    state: _TurnState,
    *,
    history: HistoryStore,
    attachment_store: AttachmentStore | None,
    attachment_catalog: SessionAttachmentCatalog | None,
    begin_epoch: bool,
) -> AsyncIterator[AgentEvent]:
    """Reload chat history and rebuild system/provider messages after compaction."""
    if begin_epoch:
        state.epoch_tracker.begin_new_epoch()
    state.chat_messages = await _load_agent_chat_history(history, state.ctx.thread_id)
    state.ctx = await refresh_memory_index(state.ctx)
    state.admit = _admit_system_context(
        state.epoch_tracker,
        state.ctx,
        state.chat_messages,
        attachment_catalog=attachment_catalog,
    )
    for epoch_evt in _epoch_events(
        state.admit, request_id=state.ctx.request_id, thread_id=state.ctx.thread_id
    ):
        yield epoch_evt
    state.system = _append_extra_system_text(
        _system_message_from_text(state.admit.leading_system_text),
        state.turn_runtime_notes if state.turn_runtime_notes is not None else state.pre_turn_extra,
    )
    state.resolved_messages = convert_to_provider(
        state.chat_messages,
        attachment_store=attachment_store,
        session_id=state.ctx.session_id,
    )
    system_msg, admit = _require_prompt(state)
    state.provider_messages = _messages_for_provider(
        system_msg,
        state.resolved_messages,
        mid_conversation_update=admit.mid_conversation_update,
    )


async def _token_pressure_should_summarize(
    *,
    history: HistoryStore,
    thread_id: str,
    preflight: int,
    cap: int,
    window_tokens: int,
) -> bool:
    if preflight < cap:
        return False
    # Judge viability on the raw stored rows ``_summarize_history`` splits, not the
    # coalesced ``transform_context`` view in ``state.chat_messages`` (fewer rows).
    try:
        raw = await history.load(thread_id)
    except Exception:
        logger.warning(
            "summarization skipped: history load failed %s",
            kv(thread_id=thread_id),
            exc_info=True,
        )
        return False
    if _summarization_viable(raw, window_tokens=window_tokens):
        return True
    logger.debug(
        "summarization skipped: nothing compactable %s",
        kv(thread_id=thread_id, preflight=preflight, cap=cap, message_count=len(raw)),
    )
    return False


async def _preflight_prompt_tokens(
    state: _TurnState,
    *,
    provider: Provider,
    vertex_google_search: bool,
) -> int:
    state.stream_thinking = _stream_thinking_budget(
        provider, state.resolved_messages, state.ctx.config
    )
    return await _provider_prompt_input_tokens(
        provider,
        state.provider_messages,
        state.turn_tools,
        model=state.ctx.model,
        thinking_budget=state.stream_thinking,
        vertex_google_search=vertex_google_search,
        hints=_provider_call_hints(state.ctx),
    )


async def _run_history_summarization(
    state: _TurnState,
    *,
    history: HistoryStore,
    provider: Provider,
    usage: Usage,
    attachment_store: AttachmentStore | None,
    attachment_catalog: SessionAttachmentCatalog | None,
    vertex_google_search: bool,
    estimated_tokens: int,
    recount_after: bool,
    hook_manager: HookManager | None = None,
) -> AsyncIterator[AgentEvent]:
    """Shared summarize body: events, LLM compact, reload, optional post-token recount."""
    from monkeybot.observability.spans import set_summarize_turns, span_summarize

    logger.debug(
        "summarization triggered %s",
        kv(
            request_id=state.ctx.request_id,
            thread_id=state.ctx.thread_id,
            turn=state.turn_index,
            estimated_tokens=estimated_tokens,
            message_count=len(state.chat_messages),
        ),
    )
    yield ContextSummarizing(
        request_id=state.ctx.request_id,
        estimated_tokens=estimated_tokens,
        context_window_tokens=state.ctx.context_window_tokens,
    )
    async with span_summarize(
        thread_id=state.ctx.thread_id,
        request_id=state.ctx.request_id,
    ):
        try:
            # Reload raw rows: state.chat_messages is the provider-facing view
            # and must never be persisted back via history.reset.
            turns_summarized = await _compact_history_if_needed(
                thread_id=state.ctx.thread_id,
                history=history,
                provider=provider,
                model=state.ctx.model,
                window_tokens=state.ctx.context_window_tokens,
                intent_facts=_intent_facts_from_ctx(state.ctx),
                workspace_trailer=_workspace_trailer_from_ctx(state.ctx),
            )
        except Exception:
            logger.warning(
                "summarization failed %s; continuing",
                kv(
                    request_id=state.ctx.request_id,
                    thread_id=state.ctx.thread_id,
                ),
                exc_info=True,
            )
            turns_summarized = 0
        set_summarize_turns(turns_summarized)
    await _fire_history_compacted(hook_manager, state.ctx, turns_summarized=turns_summarized)
    yield ContextSummarized(
        request_id=state.ctx.request_id,
        turns_summarized=turns_summarized,
    )
    logger.debug(
        "summarization done %s",
        kv(
            request_id=state.ctx.request_id,
            thread_id=state.ctx.thread_id,
            turn=state.turn_index,
            turns_summarized=turns_summarized,
        ),
    )
    async for evt in _refresh_prompt_after_history_change(
        state,
        history=history,
        attachment_store=attachment_store,
        attachment_catalog=attachment_catalog,
        begin_epoch=turns_summarized > 0,
    ):
        yield evt
    if recount_after or turns_summarized > 0:
        post = await _preflight_prompt_tokens(
            state,
            provider=provider,
            vertex_google_search=vertex_google_search,
        )
        usage.estimated_prompt_tokens = max(usage.estimated_prompt_tokens, post)
        yield ContextUsage(
            request_id=state.ctx.request_id,
            estimated_tokens=post,
            context_window_tokens=state.ctx.context_window_tokens,
            inner_turn=state.turn_index,
        )


async def _maybe_summarize_on_token_pressure(
    state: _TurnState,
    *,
    history: HistoryStore,
    provider: Provider,
    usage: Usage,
    attachment_store: AttachmentStore | None,
    attachment_catalog: SessionAttachmentCatalog | None,
    vertex_google_search: bool,
    preflight: int,
    cap: int,
    hook_manager: HookManager | None = None,
) -> AsyncIterator[AgentEvent]:
    """Compact when prompt tokens hit the context-window ratio threshold."""
    if not await _token_pressure_should_summarize(
        history=history,
        thread_id=state.ctx.thread_id,
        preflight=preflight,
        cap=cap,
        window_tokens=state.ctx.context_window_tokens,
    ):
        return
    async for evt in _run_history_summarization(
        state,
        history=history,
        provider=provider,
        usage=usage,
        attachment_store=attachment_store,
        attachment_catalog=attachment_catalog,
        vertex_google_search=vertex_google_search,
        estimated_tokens=preflight,
        recount_after=True,
        hook_manager=hook_manager,
    ):
        yield evt


async def _apply_history_load_max_safety(
    state: _TurnState,
    *,
    history: HistoryStore,
    attachment_store: AttachmentStore | None,
    attachment_catalog: SessionAttachmentCatalog | None,
) -> AsyncIterator[AgentEvent]:
    """Last resort after compact failure: persist a truncated tail (no silent slide)."""
    if len(state.chat_messages) <= HISTORY_LOAD_MAX:
        return
    # Truncate raw stored rows (not the provider-facing view) so pinned verdicts,
    # thinking, attachments and row_ids survive the durable reset.
    raw = await history.load(state.ctx.thread_id)
    if len(raw) <= HISTORY_LOAD_MAX:
        return
    dropped = len(raw) - HISTORY_LOAD_MAX
    truncated = truncate_history_preserving_pins(raw, max_rows=HISTORY_LOAD_MAX)
    logger.error(
        "history exceeds load max after compact attempt; truncating tail %s",
        kv(
            request_id=state.ctx.request_id,
            thread_id=state.ctx.thread_id,
            message_count=len(raw),
            load_max=HISTORY_LOAD_MAX,
            dropped=dropped,
        ),
    )
    await history.reset(state.ctx.thread_id, truncated)
    yield HarnessIntervention(
        request_id=state.ctx.request_id,
        intervention="compaction_fallback",
        inner_turn=state.turn_index,
        detail=f"dropped {dropped} messages; load_max={HISTORY_LOAD_MAX}",
    )
    async for evt in _refresh_prompt_after_history_change(
        state,
        history=history,
        attachment_store=attachment_store,
        attachment_catalog=attachment_catalog,
        begin_epoch=True,
    ):
        yield evt


async def _apply_pressure_and_before_provider(
    state: _TurnState,
    *,
    usage: Usage,
    hook_manager: HookManager | None,
    attachment_store: AttachmentStore | None,
) -> AsyncIterator[AgentEvent]:
    """Apply context-pressure shaping, BEFORE_PROVIDER hook, and SystemPromptSnapshot."""
    system_msg, admit = _require_prompt(state)
    pressure_tier = compute_context_pressure_tier(
        usage.estimated_prompt_tokens,
        state.ctx.context_window_tokens,
    )
    if pressure_tier in ("moderate", "aggressive"):
        state.resolved_messages = convert_to_provider(
            state.chat_messages,
            attachment_store=attachment_store,
            session_id=state.ctx.session_id,
            pressure_tier=pressure_tier,
            protect_recent=protect_recent_count(
                state.chat_messages,
                window_tokens=state.ctx.context_window_tokens,
            ),
        )
        state.provider_messages = _messages_for_provider(
            system_msg,
            state.resolved_messages,
            mid_conversation_update=admit.mid_conversation_update,
        )

    before_payload = await _fire_hook(
        hook_manager,
        event=HookEvent.BEFORE_PROVIDER_REQUEST,
        ctx=state.ctx,
        timeout_s=_HOOK_READ_TIMEOUT_S,
        tools=list(state.turn_tools),
        provider_messages=list(state.provider_messages),
        inner_turn=state.turn_index,
    )
    state.hold_for_hooks = bool(before_payload is not None and before_payload.hold_assistant_text)
    prev_msg_count = len(state.provider_messages)
    state.provider_messages, state.turn_tools, tools_replaced = _apply_before_provider_hook(
        before_payload, state.provider_messages, state.turn_tools
    )
    if tools_replaced:
        _mark_tools_dirty(state, "hook", state.turn_tools)
    if before_payload is not None and (
        tools_replaced or before_payload.provider_messages is not None
    ):
        logger.debug(
            "before_provider_request hook rewrote request %s",
            kv(
                request_id=state.ctx.request_id,
                thread_id=state.ctx.thread_id,
                turn=state.turn_index,
                tools_replaced=tools_replaced,
                message_count=len(state.provider_messages),
                prev_message_count=prev_msg_count,
            ),
        )

    yield SystemPromptSnapshot(
        request_id=state.ctx.request_id,
        inner_turn=state.turn_index,
        text=_system_prompt_snapshot_text(
            _snapshot_system_message(state.provider_messages, system_msg),
            admit.mid_conversation_update,
        ),
    )


async def _maybe_compact_and_shape(
    state: _TurnState,
    *,
    history: HistoryStore,
    provider: Provider,
    usage: Usage,
    hook_manager: HookManager | None,
    attachment_store: AttachmentStore | None,
    attachment_catalog: SessionAttachmentCatalog | None,
    vertex_google_search: bool,
) -> AsyncIterator[AgentEvent]:
    """Token compaction, pressure shaping, before-provider hook."""
    _require_prompt(state)

    preflight = await _preflight_prompt_tokens(
        state,
        provider=provider,
        vertex_google_search=vertex_google_search,
    )
    state.last_preflight_tokens = preflight
    usage.estimated_prompt_tokens = max(usage.estimated_prompt_tokens, preflight)
    yield ContextUsage(
        request_id=state.ctx.request_id,
        estimated_tokens=preflight,
        context_window_tokens=state.ctx.context_window_tokens,
        inner_turn=state.turn_index,
    )
    cap = max(1, int(state.ctx.context_window_tokens * SUMMARY_TRIGGER_RATIO))
    # Schedule only. The model call runs beside this turn; the turn that crosses
    # the bar applies it inside _compact_history_if_needed.
    await maybe_start(state.ctx, history, provider, preflight)
    async for evt in _maybe_summarize_on_token_pressure(
        state,
        history=history,
        provider=provider,
        usage=usage,
        attachment_store=attachment_store,
        attachment_catalog=attachment_catalog,
        vertex_google_search=vertex_google_search,
        preflight=preflight,
        cap=cap,
        hook_manager=hook_manager,
    ):
        yield evt
    async for evt in _apply_history_load_max_safety(
        state,
        history=history,
        attachment_store=attachment_store,
        attachment_catalog=attachment_catalog,
    ):
        yield evt
    async for evt in _apply_pressure_and_before_provider(
        state,
        usage=usage,
        hook_manager=hook_manager,
        attachment_store=attachment_store,
    ):
        yield evt


async def _write_transcript_provider_request(
    state: _TurnState,
    *,
    transcript_writer: TranscriptWriter | None,
) -> None:
    """Write provider-request transcript delta and clear tools_dirty when written."""
    if transcript_writer is None:
        return
    if state.provider_messages_written >= len(state.provider_messages):
        delta_messages = state.provider_messages
        message_offset = 0
        messages_reset = state.provider_messages_written > 0
    else:
        delta_messages = state.provider_messages[state.provider_messages_written :]
        message_offset = state.provider_messages_written
        messages_reset = False
    include_tools = state.turn_index == 1 or messages_reset or state.tools_dirty
    tools_include_reason: str | None = None
    tools_dirty_reason: str | None = None
    if include_tools:
        if state.turn_index == 1:
            # turn_index is the inner-loop counter, reset for every user message,
            # so this fires once per user turn — not once per session.
            tools_include_reason = "first_inner_turn"
        elif messages_reset:
            tools_include_reason = "messages_reset"
        else:
            tools_include_reason = "tools_dirty"
            tools_dirty_reason = state.tools_dirty_reason
    await transcript_writer.write_provider_request(
        request_id=state.ctx.request_id,
        inner_turn=state.turn_index,
        model=state.ctx.model,
        messages=[m.to_dict() for m in delta_messages],
        message_offset=message_offset,
        messages_reset=messages_reset,
        tools=[t.to_model_schema() for t in state.turn_tools] if include_tools else None,
        thinking_budget=state.stream_thinking,
        tools_include_reason=tools_include_reason,
        tools_dirty_reason=tools_dirty_reason,
    )
    state.provider_messages_written = len(state.provider_messages)
    state.tools_dirty = False
    state.tools_dirty_reason = None


@dataclasses.dataclass
class _StreamStats:
    """Token totals and the longest silence for one inner call, retries included."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    max_event_gap_s: float = 0.0


def _text_reached_client(state: _TurnState, mapper: ProviderStreamMapper) -> bool:
    """True when assistant text from this attempt was yielded to the client.

    Held text is still private to the loop, so a retry may discard it.
    """
    if state.hold_assistant_text:
        return False
    return bool((mapper.assistant_text or "").strip())


def _thinking_close_events(mapper: ProviderStreamMapper) -> list[AgentEvent]:
    """Close a thinking block the client saw. Drop any text-block close.

    ``finish`` also ends an open text block. On a retry that text was held or
    empty, and yielding its end would close a block the client never opened.
    """
    return [evt for evt in mapper.finish() if isinstance(evt, ThinkingBlockComplete)]


def _stream_usage_payload(stats: _StreamStats) -> dict[str, object]:
    return {
        "input_tokens": stats.input_tokens,
        "output_tokens": stats.output_tokens,
        "cached_tokens": stats.cached_tokens,
        "cache_read_tokens": stats.cache_read_tokens,
        "cache_creation_tokens": stats.cache_creation_tokens,
    }


async def _one_provider_attempt(
    state: _TurnState,
    *,
    provider: Provider,
    usage: Usage,
    stream_mapper: ProviderStreamMapper,
    hints: Any,
    vertex_google_search: bool,
    cancelled: asyncio.Event | None,
    preempt: asyncio.Event | None,
    idle: float | None,
    stats: _StreamStats,
) -> AsyncIterator[AgentEvent]:
    """Read one provider stream into ``stream_mapper`` and ``state``.

    Raises whatever the stream raises, including :class:`StreamStalledError`
    when ``idle`` seconds pass with no event. Does not retry.
    """
    clock = asyncio.get_running_loop().time
    last_event_at = clock()
    async with aclosing(
        cast(
            Any,
            provider_stream(
                provider,
                state.provider_messages,
                state.turn_tools,
                model=state.ctx.model,
                thinking_budget=state.stream_thinking,
                vertex_google_search=vertex_google_search,
                hints=hints,
            ),
        )
    ) as stream:
        stream_iter = stream.__aiter__()
        while True:
            try:
                ev = await race_preempt(
                    stream_iter.__anext__(),
                    preempt,
                    cancelled=cancelled,
                    idle_timeout_s=idle,
                )
            except StopAsyncIteration:
                break
            now = clock()
            stats.max_event_gap_s = max(stats.max_event_gap_s, now - last_event_at)
            last_event_at = now
            if isinstance(ev, UsageEvent):
                _merge_usage_event(usage, ev)
                usage.cost_usd += estimate_cost(
                    state.ctx.model,
                    ev.input_tokens,
                    ev.output_tokens,
                    cache_read_tokens=ev.cache_read_tokens,
                    cache_creation_tokens=ev.cache_creation_tokens,
                    cache_retention=hints.cache_retention,
                )
                stats.input_tokens += ev.input_tokens
                stats.output_tokens += ev.output_tokens
                stats.cached_tokens += ev.cached_tokens
                stats.cache_read_tokens += ev.cache_read_tokens
                stats.cache_creation_tokens += ev.cache_creation_tokens
                continue
            if isinstance(ev, Done):
                for aev in _hold_or_pass_text(state, stream_mapper.map(ev)):
                    yield aev
                break
            for aev in _hold_or_pass_text(state, stream_mapper.map(ev)):
                yield aev
    for aev in _hold_or_pass_text(state, stream_mapper.finish()):
        yield aev
    state.pending = stream_mapper.pending
    state.assistant_text = stream_mapper.assistant_text
    state.thinking_text = stream_mapper.thinking_text
    state.thinking_signature = stream_mapper.thinking_signature
    state.stream_truncated = stream_mapper.stream_truncated


@dataclasses.dataclass(frozen=True, kw_only=True)
class _AttemptChoice:
    """What the loop does with one failed provider attempt."""

    action: Literal["retry", "resume", "give_up"]
    retries_used: int
    stall_retries_used: int
    intervention: str = ""
    detail: str = ""
    notice: str = ""
    attempt: int = 0
    limit: int = 0
    delay_s: float = 0.0


def _recover_failed_attempt(
    state: _TurnState,
    exc: Exception,
    *,
    policy: Any,
    stats: _StreamStats,
    retries_used: int,
    stall_retries_used: int,
    stream_mapper: ProviderStreamMapper,
) -> _AttemptChoice:
    """Classify ``exc`` and count it against the retry or stall budget."""
    if isinstance(exc, StreamStalledError):
        stats.max_event_gap_s = max(stats.max_event_gap_s, exc.idle_timeout_s)
    failure = classify(exc)
    action = recovery(
        failure,
        policy,
        retries_used=retries_used,
        stall_retries_used=stall_retries_used,
        resumes_used=state.provider_resumes_used,
        text_reached_client=_text_reached_client(state, stream_mapper),
    )
    if action == "give_up":
        return _AttemptChoice(
            action="give_up",
            retries_used=retries_used,
            stall_retries_used=stall_retries_used,
            detail=failure.kind,
        )
    if action == "resume":
        return _AttemptChoice(
            action="resume",
            retries_used=retries_used,
            stall_retries_used=stall_retries_used,
            detail=failure.kind,
        )
    if failure.kind == "stall":
        stall_retries_used += 1
        attempt = stall_retries_used
        limit = policy.stall_retries
        intervention = "stream_stall"
    else:
        retries_used += 1
        attempt = retries_used
        limit = policy.max_retries
        intervention = "provider_retry"
    delay = policy.delay_s(attempt=attempt, failure=failure)
    return _AttemptChoice(
        action="retry",
        retries_used=retries_used,
        stall_retries_used=stall_retries_used,
        intervention=intervention,
        detail=failure.kind,
        notice=retry_notice(failure, attempt=attempt, limit=limit, delay_s=delay),
        attempt=attempt,
        limit=limit,
        delay_s=delay,
    )


async def _resume_failed_attempt(
    state: _TurnState,
    exc: Exception,
    *,
    hook_manager: HookManager | None,
    stream_mapper: ProviderStreamMapper,
    kind: str,
) -> AsyncIterator[AgentEvent]:
    """Keep text the client saw and tell the loop to continue it next turn."""
    state.provider_resumes_used += 1
    for aev in _hold_or_pass_text(state, stream_mapper.finish()):
        yield aev
    _sync_stream_mapper_text(state, stream_mapper)
    state.provider_stream_failed = True
    state.action = "resume"
    error_text = _provider_error_text(exc)
    await _fire_after_provider_response(
        hook_manager,
        ctx=state.ctx,
        inner_turn=state.turn_index,
        assistant_text=state.assistant_text,
        thinking_text=state.thinking_text,
        tool_requests=[
            {"call_id": call.call_id, "name": call.name, "args": dict(call.args)}
            for call in state.pending.values()
        ],
        provider_error=error_text,
    )
    yield HarnessIntervention(
        request_id=state.ctx.request_id,
        intervention="provider_resume",
        inner_turn=state.turn_index,
        detail=kind,
    )
    yield SystemNotificationEvent(
        request_id=state.ctx.request_id,
        notification_type="thinkingMessage",
        msg=RESUME_NOTICE,
    )
    logger.warning(
        "provider stream resume %s",
        kv(
            request_id=state.ctx.request_id,
            thread_id=state.ctx.thread_id,
            turn=state.turn_index,
            kind=kind,
            partial_chars=len(state.assistant_text or ""),
            resumes_used=state.provider_resumes_used,
            error=error_text,
        ),
    )


async def _consume_provider_stream_body(
    state: _TurnState,
    *,
    provider: Provider,
    usage: Usage,
    hook_manager: HookManager | None,
    transcript_writer: TranscriptWriter | None,
    vertex_google_search: bool,
    cancelled: asyncio.Event | None = None,
    preempt: asyncio.Event | None = None,
) -> AsyncIterator[AgentEvent]:
    """Consume provider stream, write response transcript, fire after-response hook.

    A transient failure is sent again when no assistant text reached the client,
    or kept and continued on the next inner turn when it did. Sets
    ``state.action = "return"`` on cancel or a final provider error,
    ``state.action = "preempt"`` when a promoted steer aborts the stream, and
    ``state.action = "resume"`` when a failed attempt's text was shown.
    """
    from monkeybot.observability.spans import set_llm_io, set_llm_usage, span_llm

    stats = _StreamStats()
    hints = _provider_call_hints(state.ctx)
    policy = policy_for(state.ctx.config)
    idle = idle_timeout_s(state.ctx.config)
    stream_mapper = ProviderStreamMapper(state.ctx.request_id)
    retries_used = 0
    stall_retries_used = 0
    try:
        async with span_llm(ctx=state.ctx, vertex_google_search=vertex_google_search):
            while True:
                stream_mapper = ProviderStreamMapper(state.ctx.request_id)
                try:
                    async for aev in _one_provider_attempt(
                        state,
                        provider=provider,
                        usage=usage,
                        stream_mapper=stream_mapper,
                        hints=hints,
                        vertex_google_search=vertex_google_search,
                        cancelled=cancelled,
                        preempt=preempt,
                        idle=idle,
                        stats=stats,
                    ):
                        yield aev
                    break
                except (asyncio.CancelledError, StepPreemptedError):
                    raise
                except Exception as exc:
                    choice = _recover_failed_attempt(
                        state,
                        exc,
                        policy=policy,
                        stats=stats,
                        stream_mapper=stream_mapper,
                        retries_used=retries_used,
                        stall_retries_used=stall_retries_used,
                    )
                    if choice.action == "give_up":
                        raise
                    if choice.action == "resume":
                        async for aev in _resume_failed_attempt(
                            state,
                            exc,
                            hook_manager=hook_manager,
                            stream_mapper=stream_mapper,
                            kind=choice.detail,
                        ):
                            yield aev
                        return
                    retries_used = choice.retries_used
                    stall_retries_used = choice.stall_retries_used
                    for aev in _thinking_close_events(stream_mapper):
                        yield aev
                    # A cancel or steer during the wait must not persist this attempt.
                    _discard_held_text(state)
                    stream_mapper = ProviderStreamMapper(state.ctx.request_id)
                    yield HarnessIntervention(
                        request_id=state.ctx.request_id,
                        intervention=choice.intervention,
                        inner_turn=state.turn_index,
                        detail=choice.detail,
                    )
                    yield SystemNotificationEvent(
                        request_id=state.ctx.request_id,
                        notification_type="thinkingMessage",
                        msg=choice.notice,
                    )
                    logger.warning(
                        "provider stream retry %s",
                        kv(
                            request_id=state.ctx.request_id,
                            thread_id=state.ctx.thread_id,
                            turn=state.turn_index,
                            kind=choice.detail,
                            attempt=choice.attempt,
                            max_attempts=choice.limit,
                            delay_s=round(choice.delay_s, 3),
                            max_event_gap_s=round(stats.max_event_gap_s, 3),
                            error=_provider_error_text(exc),
                        ),
                    )
                    await race_preempt(
                        asyncio.sleep(choice.delay_s),
                        preempt,
                        cancelled=cancelled,
                    )
            # A completed send can recover from a later size rejection.
            state.overflow_recovered = False
            set_llm_usage(
                input_tokens=stats.input_tokens,
                output_tokens=stats.output_tokens,
                cached_tokens=stats.cached_tokens,
                cache_read_tokens=stats.cache_read_tokens,
                cache_creation_tokens=stats.cache_creation_tokens,
            )
            set_llm_io(
                prompt=_provider_messages_prompt_summary(state.provider_messages),
                completion=state.assistant_text or "",
            )
            actual_prompt = (
                stats.input_tokens + stats.cache_read_tokens + stats.cache_creation_tokens
            )
            if state.last_preflight_tokens > 0 and actual_prompt > 0:
                if provider.name in ("bedrock", "claude", "vertex-claude"):
                    note_anthropic_token_estimate_observation(
                        estimated=state.last_preflight_tokens,
                        actual=actual_prompt,
                    )
                logger.debug(
                    "prompt token preflight vs actual %s",
                    kv(
                        request_id=state.ctx.request_id,
                        thread_id=state.ctx.thread_id,
                        turn=state.turn_index,
                        preflight=state.last_preflight_tokens,
                        actual_prompt=actual_prompt,
                        context_window=state.ctx.context_window_tokens,
                        provider=provider.name,
                    ),
                )
                if actual_prompt > state.ctx.context_window_tokens:
                    logger.warning(
                        "provider-reported prompt exceeds configured context_window %s",
                        kv(
                            request_id=state.ctx.request_id,
                            actual_prompt=actual_prompt,
                            context_window=state.ctx.context_window_tokens,
                            model=state.ctx.model,
                        ),
                    )
        if transcript_writer is not None:
            await transcript_writer.write_provider_response(
                request_id=state.ctx.request_id,
                inner_turn=state.turn_index,
                model=state.ctx.model,
                text=state.assistant_text,
                thinking=state.thinking_text,
                tool_requests=[
                    {"call_id": tc.call_id, "name": tc.name, "args": tc.args}
                    for tc in state.pending.values()
                ],
                usage=_stream_usage_payload(stats),
            )
        logger.debug(
            "provider stream done %s",
            kv(
                request_id=state.ctx.request_id,
                thread_id=state.ctx.thread_id,
                turn=state.turn_index,
                llm_input=stats.input_tokens,
                llm_output=stats.output_tokens,
                llm_cached=stats.cached_tokens,
                tool_calls=len(state.pending),
                max_event_gap_s=round(stats.max_event_gap_s, 3),
            ),
        )
        await _fire_after_provider_response(
            hook_manager,
            ctx=state.ctx,
            inner_turn=state.turn_index,
            assistant_text=state.assistant_text,
            thinking_text=state.thinking_text,
            tool_requests=[
                {"call_id": tc.call_id, "name": tc.name, "args": dict(tc.args)}
                for tc in state.pending.values()
            ],
            usage=cast(dict[str, int], _stream_usage_payload(stats)),
        )
    except asyncio.CancelledError:
        state.hold_assistant_text = False
        for aev in [*state.held_text_events, *stream_mapper.finish()]:
            yield aev
        state.held_text_events.clear()
        _sync_stream_mapper_text(state, stream_mapper)
        logger.info(
            "provider stream cancelled %s",
            kv(
                request_id=state.ctx.request_id,
                thread_id=state.ctx.thread_id,
                turn=state.turn_index,
                partial_chars=len(state.assistant_text or ""),
                had_thinking=bool((state.thinking_text or "").strip()),
            ),
        )
        if transcript_writer is not None:
            await transcript_writer.write_provider_response(
                request_id=state.ctx.request_id,
                inner_turn=state.turn_index,
                model=state.ctx.model,
                text=state.assistant_text,
                thinking=state.thinking_text,
                tool_requests=[
                    {"call_id": tc.call_id, "name": tc.name, "args": tc.args}
                    for tc in state.pending.values()
                ],
                usage=_stream_usage_payload(stats),
            )
        await _fire_after_provider_response(
            hook_manager,
            ctx=state.ctx,
            inner_turn=state.turn_index,
            assistant_text=state.assistant_text,
            thinking_text=state.thinking_text,
            provider_error="Request cancelled",
        )
        yield Error(request_id=state.ctx.request_id, error="Request cancelled")
        state.needs_followup_after_tools = False
        state.action = "return"
        return
    except StepPreemptedError:
        state.hold_assistant_text = False
        for aev in [*state.held_text_events, *stream_mapper.finish()]:
            yield aev
        state.held_text_events.clear()
        _sync_stream_mapper_text(state, stream_mapper)
        logger.info(
            "provider stream preempted %s",
            kv(
                request_id=state.ctx.request_id,
                thread_id=state.ctx.thread_id,
                turn=state.turn_index,
                partial_chars=len(state.assistant_text or ""),
            ),
        )
        if transcript_writer is not None:
            await transcript_writer.write_provider_response(
                request_id=state.ctx.request_id,
                inner_turn=state.turn_index,
                model=state.ctx.model,
                text=state.assistant_text,
                thinking=state.thinking_text,
                tool_requests=[
                    {"call_id": tc.call_id, "name": tc.name, "args": tc.args}
                    for tc in state.pending.values()
                ],
                usage=_stream_usage_payload(stats),
            )
        await _fire_after_provider_response(
            hook_manager,
            ctx=state.ctx,
            inner_turn=state.turn_index,
            assistant_text=state.assistant_text,
            thinking_text=state.thinking_text,
        )
        state.action = "preempt"
        return
    except Exception as exc:
        state.hold_assistant_text = False
        for aev in [*state.held_text_events, *stream_mapper.finish()]:
            yield aev
        state.held_text_events.clear()
        _sync_stream_mapper_text(state, stream_mapper)
        if _recover_context_overflow(state, exc, stream_mapper):
            return
        logger.exception(
            "provider stream failed %s",
            kv(
                request_id=state.ctx.request_id,
                thread_id=state.ctx.thread_id,
                model=state.ctx.model,
                partial_chars=len(state.assistant_text or ""),
            ),
        )
        error_text = _provider_error_text(exc)
        if is_context_overflow(exc) and not (stream_mapper.assistant_text or "").strip():
            error_text = CONTEXT_OVERFLOW_ERROR
        await _fire_after_provider_response(
            hook_manager,
            ctx=state.ctx,
            inner_turn=state.turn_index,
            assistant_text=state.assistant_text,
            thinking_text=state.thinking_text,
            provider_error=error_text,
        )
        yield Error(request_id=state.ctx.request_id, error=error_text)
        state.needs_followup_after_tools = False
        state.provider_stream_failed = True
        state.action = "return"
        return


def _recover_context_overflow(
    state: _TurnState,
    exc: Exception,
    stream_mapper: ProviderStreamMapper,
) -> bool:
    """True when a size rejection will be answered by compacting and sending again.

    One recovery per send. A rejection that already produced assistant text, or a
    second rejection of the same send, is reported by the caller. The raw provider
    text is logged and is not the message the user sees.
    """
    if not is_context_overflow(exc):
        return False
    logger.warning(
        "provider rejected the request for size %s",
        kv(
            request_id=state.ctx.request_id,
            thread_id=state.ctx.thread_id,
            turn=state.turn_index,
            error=_provider_error_text(exc),
        ),
    )
    if (stream_mapper.assistant_text or "").strip() or state.overflow_recovered:
        return False
    state.overflow_recovered = True
    state.action = "overflow"
    state.needs_followup_after_tools = False
    return True


def _provider_error_text(exc: Exception) -> str:
    """Error text for a failed provider call; never blank (``httpx.ReadTimeout`` stringifies to "")."""
    return str(exc).strip() or f"Provider request failed ({type(exc).__name__})"


def _thinking_block(state: _TurnState) -> ContentBlock | None:
    """Wrap the turn's accumulated thinking text, if any, for persistence.

    Emptiness uses ``.strip()``, but the persisted text stays unmodified so
    Anthropic/Gemini signature replay after tool use still matches the
    exact content the provider signed.
    """
    raw = state.thinking_text or ""
    if not raw.strip():
        return None
    return ThinkingBlock(
        thinking=raw,
        signature=state.thinking_signature or "",
    )


def _sync_stream_mapper_text(state: _TurnState, stream_mapper: ProviderStreamMapper) -> None:
    """Copy streamed text/thinking/pending into turn state after an abort mid-stream.

    Pending tool calls are copied so ``_persist_partial_assistant_on_abort`` can
    settle ToolRequest + cancel ToolResponse pairs and keep history consistent
    with any tool-call deltas the client already saw.
    """
    state.assistant_text = stream_mapper.assistant_text
    state.thinking_text = stream_mapper.thinking_text
    state.thinking_signature = stream_mapper.thinking_signature
    state.stream_truncated = stream_mapper.stream_truncated
    state.pending = dict(stream_mapper.pending)


async def _persist_partial_assistant_on_abort(
    state: _TurnState,
    *,
    history: HistoryStore,
    last_assistant: list[str],
    tool_result_text: Callable[[str], str] | None = None,
) -> None:
    """Persist already-streamed assistant output so Stop/errors keep model context.

    Early ``action=\"return\"`` and post-stream cancel skip the normal final-text /
    tool-dispatch paths. Persist thinking + text the user already saw, and when
    tool calls were finalized in the stream, settle matching cancel envelopes so
    history never holds bare ``ToolRequest`` blocks.
    """
    cleaned = (state.assistant_text or "").strip()
    ordered = list(state.pending.values())
    assist_blocks: list[ContentBlock] = []
    thinking = _thinking_block(state)
    if thinking is not None:
        assist_blocks.append(thinking)
    if cleaned:
        assist_blocks.append(Text(text=cleaned))
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
    if not assist_blocks:
        return
    if tool_result_text is not None:
        tool_text_fn = tool_result_text
    elif state.provider_stream_failed:
        tool_text_fn = interrupted_tool_result_text
    else:
        tool_text_fn = cancelled_tool_result_text
    await persist_message(
        history,
        Message(role="assistant", content=assist_blocks),
        thread_id=state.ctx.thread_id,
        turn_id=state.ctx.request_id,
        memory=state.ctx.memory,
        # Skip memory ingest when only tool-call blocks were finalized — the
        # cancel/interrupt envelopes carry the durable tool facts for this turn.
        ingest=bool(cleaned) and not ordered,
    )
    if ordered:
        cancel_responses: list[ContentBlock] = [
            ToolResponse(
                id=c.call_id,
                tool_name=c.name,
                result=[Text(text=tool_text_fn(c.name))],
                is_error=True,
            )
            for c in ordered
        ]
        await persist_message(
            history,
            Message(role="user", content=cancel_responses),
            thread_id=state.ctx.thread_id,
            turn_id=state.ctx.request_id,
            memory=state.ctx.memory,
            ingest=False,
        )
        state.pending.clear()
    if cleaned:
        last_assistant[0] = cleaned
        state.turn_output_text = cleaned


async def _scan_outbound_before_provider_call(state: _TurnState) -> AsyncIterator[AgentEvent]:
    """Credential broker phase 5.3: scan + redact new assistant text / tool
    results in ``state.provider_messages`` before they are ever sent to the
    LLM provider. Runs before the transcript write too, so a caught secret
    never lands durably on disk either.

    On a hit during a routine (``MONKEYBOT_RUN_KIND=routine``), sets
    ``state.action = "return"`` so the caller ends the turn without calling
    the provider at all — an interactive chat instead gets the notice and
    continues with the redacted messages.
    """
    if state.provider_messages_scanned >= len(state.provider_messages):
        delta = state.provider_messages
        offset = 0
    else:
        delta = state.provider_messages[state.provider_messages_scanned :]
        offset = state.provider_messages_scanned
    if not delta:
        return
    redacted, events, should_end = await _scan_and_redact_outbound(state.ctx.request_id, delta)
    state.provider_messages[offset:] = redacted
    state.provider_messages_scanned = len(state.provider_messages)
    for evt in events:
        yield evt
    if should_end:
        state.action = "return"


_TEXT_EVENT_TYPES = (AssistantTextStarted, AssistantDelta, AssistantTextEnded)


def _hold_or_pass_text(state: _TurnState, events: Sequence[AgentEvent]) -> list[AgentEvent]:
    """Buffer assistant text events while ``hold_assistant_text`` is set."""
    if not state.hold_assistant_text:
        return list(events)
    passed: list[AgentEvent] = []
    for evt in events:
        if isinstance(evt, _TEXT_EVENT_TYPES):
            state.held_text_events.append(evt)
        else:
            passed.append(evt)
    return passed


def _release_held_text(state: _TurnState) -> list[AgentEvent]:
    """Return held assistant text events and stop holding."""
    state.hold_assistant_text = False
    released = state.held_text_events
    state.held_text_events = []
    return released


def _reset_uncommitted_reply(state: _TurnState) -> None:
    """Drop assistant output this attempt has not committed to history.

    A rejected final answer, a reply blocked because jobs are still running, and
    the previous attempt's text must not be what abort persistence writes. The
    next provider stream fills these fields from its own events.
    """
    state.hold_assistant_text = False
    state.held_text_events.clear()
    state.assistant_text = ""
    state.thinking_text = ""
    state.thinking_signature = None
    state.pending = {}
    state.stream_truncated = False


def _discard_held_text(state: _TurnState) -> None:
    """Drop a reply the user was not shown and that must not be persisted."""
    _reset_uncommitted_reply(state)


async def _stream_provider_turn(
    state: _TurnState,
    *,
    provider: Provider,
    usage: Usage,
    hook_manager: HookManager | None,
    transcript_writer: TranscriptWriter | None,
    vertex_google_search: bool,
    tool_executor: object | None = None,
    cancelled: asyncio.Event | None = None,
    preempt: asyncio.Event | None = None,
) -> AsyncIterator[AgentEvent]:
    """Stream provider events; fill ``state.pending`` / text fields. May set action=return."""
    # Previous attempt's text is not this call's partial output. Clearing it here
    # covers every early return, including a rejected answer the gate already dropped.
    _reset_uncommitted_reply(state)
    try:
        state.hold_assistant_text = bool(_running_background_jobs(tool_executor))
    except Exception:
        # Unknown job state: hold, the turn-end check fails closed anyway.
        state.hold_assistant_text = True
    state.hold_assistant_text = state.hold_assistant_text or state.hold_for_hooks
    async for evt in _scan_outbound_before_provider_call(state):
        yield evt
    if state.action == "return":
        return
    await _write_transcript_provider_request(state, transcript_writer=transcript_writer)
    async for evt in _consume_provider_stream_body(
        state,
        provider=provider,
        usage=usage,
        hook_manager=hook_manager,
        transcript_writer=transcript_writer,
        vertex_google_search=vertex_google_search,
        cancelled=cancelled,
        preempt=preempt,
    ):
        yield evt


def _running_background_jobs(tool_executor: object) -> list[object]:
    fn = getattr(tool_executor, "running_background_jobs", None)
    if not callable(fn):
        return []
    try:
        jobs = fn()
    except Exception:
        logger.warning("running_background_jobs failed", exc_info=True)
        raise
    return list(jobs or [])


async def _nudge_or_error_on_background_jobs(
    state: _TurnState,
    tool_executor: object | None,
) -> AsyncIterator[AgentEvent]:
    """Stop a final answer while jobs run, or when their status cannot be read.

    Sets ``state.action`` to ``continue`` or ``break`` when the turn must not
    finish. Leaves ``state.action`` unchanged when it may.
    """
    if tool_executor is None:
        return
    try:
        running_jobs = _running_background_jobs(tool_executor)
    except Exception:
        for evt in _release_held_text(state):
            yield evt
        state.background_jobs_reported = True
        yield Error(
            request_id=state.ctx.request_id,
            error=jobs_check_failed_error(),
        )
        state.needs_followup_after_tools = False
        state.action = "break"
        return
    if not running_jobs:
        return
    if state.turn_index < state.effective_max and state.background_job_nudges_left > 0:
        # The blocked reply is never shown; the model retries after the nudge.
        _discard_held_text(state)
        state.background_job_nudges_left -= 1
        note = jobs_still_running_note(running_jobs)
        state.pre_tool_extra_next = _combine_extras(state.pre_tool_extra_next, note)
        yield HarnessIntervention(
            request_id=state.ctx.request_id,
            intervention="background_jobs_nudge",
            inner_turn=state.turn_index,
            detail=note,
        )
        logger.warning(
            "final answer blocked; background jobs still running %s",
            kv(
                request_id=state.ctx.request_id,
                thread_id=state.ctx.thread_id,
                turn=state.turn_index,
                jobs=len(running_jobs),
                nudges_left=state.background_job_nudges_left,
            ),
        )
        state.needs_followup_after_tools = False
        state.action = "continue"
        return
    for evt in _release_held_text(state):
        yield evt
    state.background_jobs_reported = True
    yield Error(
        request_id=state.ctx.request_id,
        error=jobs_will_be_killed_error(running_jobs),
    )
    state.needs_followup_after_tools = False
    state.action = "break"


async def _gate_final_answer(
    state: _TurnState,
    *,
    hook_manager: HookManager | None,
    candidate: str,
) -> AsyncIterator[AgentEvent]:
    """Let ``PRE_FINAL_ANSWER`` hooks refuse ``candidate`` once the tools are done.

    On refusal sets ``state.action = "continue"`` with the hook's note queued as the next
    runtime note. A hook that errors or times out is a pass-through (``_fire_hook``
    never raises), and refusals are capped per message so a hook cannot spin the turn.
    """
    if (
        hook_manager is None
        or not candidate
        or state.final_answer_rejections_left <= 0
        or state.turn_index >= state.effective_max
    ):
        return
    payload = await _fire_hook(
        hook_manager,
        event=HookEvent.PRE_FINAL_ANSWER,
        ctx=state.ctx,
        timeout_s=_HOOK_READ_TIMEOUT_S,
        assistant_text=candidate,
        inner_turn=state.turn_index,
    )
    note = (payload.reject_final_answer or "").strip() if payload is not None else ""
    if not note:
        return
    # The refused answer is never shown or persisted; the model answers again after the note.
    _discard_held_text(state)
    state.final_answer_rejections_left -= 1
    state.pre_tool_extra_next = _combine_extras(state.pre_tool_extra_next, note)
    yield HarnessIntervention(
        request_id=state.ctx.request_id,
        intervention="final_answer_gate",
        inner_turn=state.turn_index,
        detail=note,
    )
    logger.warning(
        "final answer rejected by hook %s",
        kv(
            request_id=state.ctx.request_id,
            thread_id=state.ctx.thread_id,
            turn=state.turn_index,
            rejections_left=state.final_answer_rejections_left,
        ),
    )
    state.needs_followup_after_tools = False
    state.action = "continue"


async def _handle_empty_or_final_text(
    state: _TurnState,
    *,
    history: HistoryStore,
    last_assistant: list[str],
    tool_executor: object | None = None,
    hook_manager: HookManager | None = None,
) -> AsyncIterator[AgentEvent]:
    """No tool calls: final assistant write, empty-completion retry, or exhausted Error."""
    cleaned_text = (state.assistant_text or "").strip()
    async for evt in _nudge_or_error_on_background_jobs(state, tool_executor):
        yield evt
    if state.action in ("continue", "break"):
        return
    # A write-without-read reminder stays until that path is read. Ending on the
    # text reply would drop the later inner turns that still have to read it.
    # Tool-suppressing recovery (replan, doom loop) still owns that turn.
    unresolved_write = _write_nudge_unresolved(state) and not state.tools_suppressed
    if cleaned_text and (state.verifier_replan_turn or unresolved_write):
        for evt in _release_held_text(state):
            yield evt
        assist_blocks: list[ContentBlock] = []
        thinking = _thinking_block(state)
        if thinking is not None:
            assist_blocks.append(thinking)
        assist_blocks.append(Text(text=cleaned_text))
        # The next provider call reloads history, so this write cannot be deferred.
        await persist_message(
            history,
            Message(role="assistant", content=assist_blocks),
            thread_id=state.ctx.thread_id,
            turn_id=state.ctx.request_id,
            memory=state.ctx.memory,
            ingest=True,
        )
        last_assistant[0] = cleaned_text
        state.turn_output_text = cleaned_text
        state.verifier_replan_turn = False
        state.needs_followup_after_tools = False
        state.action = "continue"
        return
    async for evt in _gate_final_answer(state, hook_manager=hook_manager, candidate=cleaned_text):
        yield evt
    if state.action == "continue":
        return
    for evt in _release_held_text(state):
        yield evt
    if cleaned_text:
        # Fire-and-forget: this is the final assistant turn (we break
        # right after), so nothing reads it again in-loop. Keeping it
        # off the await path means the Firestore write does not delay
        # TurnComplete / the user-visible reply. Awaited at the tail.
        final_blocks: list[ContentBlock] = []
        thinking = _thinking_block(state)
        if thinking is not None:
            final_blocks.append(thinking)
        final_blocks.append(Text(text=cleaned_text))
        state.assistant_write_task = asyncio.create_task(
            persist_message(
                history,
                Message(role="assistant", content=final_blocks),
                thread_id=state.ctx.thread_id,
                turn_id=state.ctx.request_id,
                memory=state.ctx.memory,
                ingest=True,
            )
        )
        last_assistant[0] = cleaned_text
        state.turn_output_text = cleaned_text
        state.needs_followup_after_tools = False
        state.action = "break"
        return
    # Model returned no text (or only whitespace) and no tool calls.
    # Post-tool silence: keep retrying until the turn budget so the
    # user never sees tools then nothing — logged each time so
    # scorecards / transcripts surface the stall, but not yielded
    # as an Error since the harness is actively recovering and the
    # user hasn't seen a failure yet. First-turn / thinking-only
    # silence: bounded recovery (same non-fatal logging), then an
    # exhausted Error once retries run out and the turn ends.
    rows = await _load_agent_chat_history(history, state.ctx.thread_id)
    owes_tool_followup = state.needs_followup_after_tools or (
        bool(rows)
        and rows[-1].role == "user"
        and any(isinstance(b, ToolResponse) for b in rows[-1].content)
    )
    retry_empty = False
    recovery_note = _EMPTY_COMPLETION_RECOVERY_NOTE
    log_msg = "empty model completion; retrying %s"
    log_fields: dict[str, Any] = {
        "request_id": state.ctx.request_id,
        "thread_id": state.ctx.thread_id,
        "turn": state.turn_index,
        "had_thinking": bool((state.thinking_text or "").strip()),
    }
    if owes_tool_followup and state.turn_index < state.effective_max:
        if state.post_tool_empty_retries_left > 0:
            state.post_tool_empty_retries_left -= 1
            retry_empty = True
            recovery_note = _POST_TOOL_EMPTY_COMPLETION_NOTE
            log_msg = "empty model completion after tools; retrying %s"
            log_fields["post_tool_retries_left"] = state.post_tool_empty_retries_left
    elif state.empty_completion_retries_left > 0 and state.turn_index < state.effective_max:
        state.empty_completion_retries_left -= 1
        retry_empty = True
        state.needs_followup_after_tools = False
        log_fields["retries_left"] = state.empty_completion_retries_left
    if retry_empty:
        state.pre_tool_extra_next = _combine_extras(
            state.pre_tool_extra_next,
            recovery_note,
        )
        yield HarnessIntervention(
            request_id=state.ctx.request_id,
            intervention="post_tool_empty" if owes_tool_followup else "empty_completion",
            inner_turn=state.turn_index,
            detail="retry",
        )
        logger.warning(log_msg, kv(**log_fields))
        state.action = "continue"
        return
    logger.warning("empty model completion; ending turn %s", kv(**log_fields))
    yield HarnessIntervention(
        request_id=state.ctx.request_id,
        intervention="post_tool_empty" if owes_tool_followup else "empty_completion",
        inner_turn=state.turn_index,
        detail="exhausted",
    )
    yield Error(
        request_id=state.ctx.request_id,
        error=_EMPTY_COMPLETION_EXHAUSTED_ERROR,
    )
    state.needs_followup_after_tools = False
    state.action = "break"


async def _append_tool_requests_and_dispatch(
    state: _TurnState,
    *,
    history: HistoryStore,
    inspectors: list[ToolInspector],
    tool_executor: ToolExecutorPort,
    cancelled: asyncio.Event | None,
    usage: Usage,
    preempt: asyncio.Event | None = None,
    provider: Provider,
    hook_manager: HookManager | None,
    attachment_store: AttachmentStore | None,
    attachment_catalog: SessionAttachmentCatalog | None,
    vertex_google_search: bool,
) -> AsyncIterator[AgentEvent]:
    """Append assistant tool-request message and dispatch the tool batch.

    Yields tool-batch events. On abort sets ``state.action = "return"`` so the
    caller can exit the core loop.
    """
    # Preserve stream insertion order so Gemini thought_signature stays on the
    # first functionCall part (parallel calls: only the first carries a sig).
    ordered = list(state.pending.values())
    assist_blocks: list[ContentBlock] = []
    thinking = _thinking_block(state)
    if thinking is not None:
        assist_blocks.append(thinking)
    prose = (state.assistant_text or "").strip()
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
    await persist_message(
        history,
        Message(role="assistant", content=assist_blocks),
        thread_id=state.ctx.thread_id,
        turn_id=state.ctx.request_id,
        memory=state.ctx.memory,
        ingest=False,
    )

    state.needs_followup_after_tools = True

    batch_state = ToolBatchState(
        ctx=state.ctx,
        tools_dirty=state.tools_dirty,
        tools_dirty_reason=state.tools_dirty_reason,
        pre_tool_extra_next=state.pre_tool_extra_next,
    )
    async for evt in dispatch_tool_batch(
        ordered=ordered,
        stream_truncated=state.stream_truncated,
        state=batch_state,
        doom_tracker=state.doom_tracker,
        inspectors=inspectors,
        tool_executor=tool_executor,
        cancelled=cancelled,
        preempt=preempt,
        history=history,
        usage=usage,
        provider=provider,
        hook_manager=hook_manager,
        turn_index=state.turn_index,
        pre_turn_extra=state.pre_turn_extra,
        attachment_store=attachment_store,
        attachment_catalog=attachment_catalog,
        vertex_google_search=vertex_google_search,
        epoch_tracker=state.epoch_tracker,
    ):
        yield evt
    state.ctx = batch_state.ctx
    state.tools_dirty = batch_state.tools_dirty
    state.tools_dirty_reason = batch_state.tools_dirty_reason
    state.pre_tool_extra_next = batch_state.pre_tool_extra_next
    if batch_state.aborted:
        state.needs_followup_after_tools = batch_state.needs_followup_after_tools
        state.action = "return"
    elif batch_state.preempted:
        state.action = "preempt"


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
    last_assistant: list[str],
    attachment_store: AttachmentStore | None = None,
    attachment_catalog: SessionAttachmentCatalog | None = None,
    transcript_writer: TranscriptWriter | None = None,
    vertex_google_search: bool = False,
    input_admission: InputAdmission | None = None,
    input_provenance: str = "human",
    summary_token: list[Any] | None = None,
) -> AsyncIterator[AgentEvent]:
    from monkeybot.observability.spans import (
        begin_turn_span,
        end_turn_span,
        set_turn_io,
        set_turn_prompt_tokens,
    )

    state = _TurnState(
        ctx=ctx,
        user_text=_user_text_from_content(user_content),
        effective_max=_effective_max_turns(max_turns, ctx.config),
        doom_tracker=_DoomLoopTracker(
            threshold=_effective_doom_loop_threshold(),
            exempt_names=_doom_loop_exempt_names(ctx.tools),
        ),
    )

    def _transcript_view() -> list[Message]:
        return state.chat_messages

    state.ctx = dataclasses.replace(state.ctx, transcript_view=_transcript_view)
    user_row_id = new_row_id()
    harness_turn = input_provenance == "harness"
    await persist_message(
        history,
        Message(role="user", content=list(user_content), row_id=user_row_id),
        thread_id=state.ctx.thread_id,
        turn_id=state.ctx.request_id,
        memory=state.ctx.memory,
        ingest=not harness_turn,
    )
    if harness_turn:
        ledger = state.ctx.goal_ledger
        if ledger is not None and state.user_text:
            from monkeybot.core.persistence.goal_ledger import Channel, Provenance

            try:
                ledger.admit(
                    state.ctx.thread_id,
                    state.user_text,
                    provenance=Provenance.HARNESS,
                    channel=Channel.FOLLOW_UP,
                    source_row_id=user_row_id,
                )
            except Exception:
                logger.warning(
                    "goal_ledger harness admit failed %s",
                    kv(thread_id=state.ctx.thread_id, request_id=state.ctx.request_id),
                    exc_info=True,
                )
    else:
        await _fire_hook(
            hook_manager,
            event=HookEvent.USER_MESSAGE,
            ctx=state.ctx,
            timeout_s=0,
            user_message=state.user_text,
            user_row_id=user_row_id,
        )

    def _preempt_event() -> asyncio.Event | None:
        if input_admission is None:
            return None
        return input_admission.preempt_event

    def _drop_steers_if_stopped() -> None:
        """Stop ends the turn. Promoted messages go back to the follow-up queue."""
        if cancelled is not None and cancelled.is_set() and input_admission is not None:
            input_admission.clear_steer()

    while state.turn_index < state.effective_max:
        if cancelled is not None and cancelled.is_set():
            _drop_steers_if_stopped()
            yield Error(request_id=state.ctx.request_id, error="Request cancelled")
            state.needs_followup_after_tools = False
            break

        async for steer_evt in _drain_steers(
            input_admission=input_admission, history=history, ctx=state.ctx
        ):
            yield steer_evt

        # Settlement barrier: fire-and-forget hooks from the prior tool batch
        # must finish (or time out) before the next provider call.
        await _drain_hook_settlement(hook_manager)

        state.verifier_replan_turn = False
        state.tools_suppressed = False
        state.turn_index += 1
        _rebind_summary_transcript(
            summary_token,
            transcript_writer,
            request_id=state.ctx.request_id,
            inner_turn=state.turn_index,
        )
        state.turn_input_text = state.user_text
        state.turn_output_text = ""
        logger.debug(
            "inner turn start %s",
            kv(
                request_id=state.ctx.request_id,
                thread_id=state.ctx.thread_id,
                turn=state.turn_index,
                estimated_prompt_tokens=usage.estimated_prompt_tokens,
            ),
        )
        turn_span = begin_turn_span(
            turn_index=state.turn_index,
            thread_id=state.ctx.thread_id,
            request_id=state.ctx.request_id,
        )
        try:
            yield Thinking(request_id=state.ctx.request_id)

            if cancelled is not None and cancelled.is_set():
                _drop_steers_if_stopped()
                yield Error(request_id=state.ctx.request_id, error="Request cancelled")
                state.needs_followup_after_tools = False
                break

            async for evt in _prepare_turn_context(
                state,
                history=history,
                hook_manager=hook_manager,
                attachment_store=attachment_store,
                attachment_catalog=attachment_catalog,
            ):
                yield evt

            async for evt in _maybe_compact_and_shape(
                state,
                history=history,
                provider=provider,
                usage=usage,
                hook_manager=hook_manager,
                attachment_store=attachment_store,
                attachment_catalog=attachment_catalog,
                vertex_google_search=vertex_google_search,
            ):
                yield evt

            async for evt in _stream_provider_turn(
                state,
                provider=provider,
                usage=usage,
                hook_manager=hook_manager,
                transcript_writer=transcript_writer,
                vertex_google_search=vertex_google_search,
                tool_executor=tool_executor,
                cancelled=cancelled,
                preempt=_preempt_event(),
            ):
                yield evt
            if state.action == "overflow":
                summarized = 0
                async for evt in _run_history_summarization(
                    state,
                    history=history,
                    provider=provider,
                    usage=usage,
                    attachment_store=attachment_store,
                    attachment_catalog=attachment_catalog,
                    vertex_google_search=vertex_google_search,
                    estimated_tokens=state.last_preflight_tokens,
                    recount_after=True,
                    hook_manager=hook_manager,
                ):
                    if isinstance(evt, ContextSummarized):
                        summarized = evt.turns_summarized
                    yield evt
                if summarized <= 0:
                    yield Error(
                        request_id=state.ctx.request_id,
                        error=CONTEXT_OVERFLOW_ERROR,
                    )
                    state.needs_followup_after_tools = False
                    break
                yield HarnessIntervention(
                    request_id=state.ctx.request_id,
                    intervention="context_overflow_recovery",
                    inner_turn=state.turn_index,
                    detail=f"turns_summarized={summarized}",
                )
                state.action = None
                continue
            if state.action == "return":
                for evt in _release_held_text(state):
                    yield evt
                # Cancel/error mid-stream skips _handle_empty_or_final_text —
                # persist any text already shown to the user before exiting.
                # Break (do not return) so freeze_attachments_in_history still runs.
                _drop_steers_if_stopped()
                state.needs_followup_after_tools = False
                await _persist_partial_assistant_on_abort(
                    state, history=history, last_assistant=last_assistant
                )
                break
            if state.action == "preempt":
                logger.info(
                    "step preempted %s",
                    kv(
                        request_id=state.ctx.request_id,
                        thread_id=state.ctx.thread_id,
                        turn=state.turn_index,
                        where="stream",
                    ),
                )
                for evt in _release_held_text(state):
                    yield evt
                preempted_calls = list(state.pending.values())
                await _persist_partial_assistant_on_abort(
                    state,
                    history=history,
                    last_assistant=last_assistant,
                    tool_result_text=preempted_tool_result_text,
                )
                for call in preempted_calls:
                    note = preempted_tool_result_text(call.name)
                    yield ToolCallResult(
                        request_id=state.ctx.request_id,
                        tool=call.name,
                        call_id=call.call_id,
                        error=note,
                    )
                state.action = None
                continue
            if state.action == "resume":
                for evt in _release_held_text(state):
                    yield evt
                await _persist_partial_assistant_on_abort(
                    state, history=history, last_assistant=last_assistant
                )
                state.provider_stream_failed = False
                state.pre_tool_extra_next = _combine_extras(
                    state.pre_tool_extra_next,
                    RESUME_NOTE,
                )
                state.action = None
                continue
            state.action = None

            if cancelled is not None and cancelled.is_set():
                # Stream already finished (and may have been shown over SSE).
                # Persist text and settle any finalized tool calls the same way
                # mid-stream abort does.
                _drop_steers_if_stopped()
                for evt in _release_held_text(state):
                    yield evt
                yield Error(request_id=state.ctx.request_id, error="Request cancelled")
                state.needs_followup_after_tools = False
                await _persist_partial_assistant_on_abort(
                    state, history=history, last_assistant=last_assistant
                )
                break

            _note_mcp_idle_turn(state, tool_executor)

            if not state.pending:
                async for evt in _handle_empty_or_final_text(
                    state,
                    history=history,
                    last_assistant=last_assistant,
                    tool_executor=tool_executor,
                    hook_manager=hook_manager,
                ):
                    yield evt
                if state.action == "continue":
                    continue
                if state.action == "break":
                    # A promote that landed on this text-only step joins the
                    # turn instead of waiting for a later one.
                    if input_admission is not None and input_admission.steer_depth > 0:
                        await _await_history_write(state.assistant_write_task)
                        state.assistant_write_task = None
                        state.action = None
                        continue
                    break

            else:
                for evt in _release_held_text(state):
                    yield evt
                async for evt in _append_tool_requests_and_dispatch(
                    state,
                    history=history,
                    inspectors=inspectors,
                    tool_executor=tool_executor,
                    cancelled=cancelled,
                    preempt=_preempt_event(),
                    usage=usage,
                    provider=provider,
                    hook_manager=hook_manager,
                    attachment_store=attachment_store,
                    attachment_catalog=attachment_catalog,
                    vertex_google_search=vertex_google_search,
                ):
                    yield evt
                if state.action == "return":
                    _drop_steers_if_stopped()
                    break
                if state.action == "preempt":
                    logger.info(
                        "step preempted %s",
                        kv(
                            request_id=state.ctx.request_id,
                            thread_id=state.ctx.thread_id,
                            turn=state.turn_index,
                            where="tools",
                        ),
                    )
                    state.action = None
                    continue

        finally:
            set_turn_prompt_tokens(usage.estimated_prompt_tokens)
            if state.turn_output_text:
                set_turn_io(output_value=state.turn_output_text)
            end_turn_span(turn_span)

    if state.needs_followup_after_tools:
        logger.warning(
            "max turns exceeded %s",
            kv(
                request_id=state.ctx.request_id,
                thread_id=state.ctx.thread_id,
                max_turns=state.effective_max,
            ),
        )

    if not state.background_jobs_reported:
        try:
            still_running = _running_background_jobs(tool_executor)
        except Exception:
            state.background_jobs_reported = True
            yield Error(
                request_id=state.ctx.request_id,
                error=jobs_check_failed_error(),
            )
        else:
            if still_running:
                state.background_jobs_reported = True
                yield Error(
                    request_id=state.ctx.request_id,
                    error=jobs_will_be_killed_error(still_running),
                )

    # Ensure the backgrounded assistant write has landed before any load/reset
    # below (freeze) so the assistant row is durable and not overwritten.
    await _await_history_write(state.assistant_write_task)

    async for verdict_evt in _drain_verdicts(
        state.ctx, history, grace_s=_verdict_grace_s(state.ctx)
    ):
        yield verdict_evt

    descriptor_events = await freeze_attachments_in_history(
        thread_id=state.ctx.thread_id,
        history=history,
        catalog=attachment_catalog,
        last_assistant_text=last_assistant[0],
    )
    for desc_evt in descriptor_events:
        yield AttachmentDescriptorEvent(
            request_id=state.ctx.request_id,
            attachment_id=desc_evt.attachment_id,
            mime_type=desc_evt.mime_type,
            filename=desc_evt.filename,
            description=desc_evt.description,
        )

    await _fire_hook(
        hook_manager,
        event=HookEvent.POST_TURN,
        ctx=state.ctx,
        timeout_s=0,
    )
    # Settlement for POST_TURN / lingering POST_TOOL runs in run() finally
    # before TurnComplete — do not drain twice here.
    if state.needs_followup_after_tools:
        yield HarnessIntervention(
            request_id=state.ctx.request_id,
            intervention="max_turns",
            inner_turn=state.turn_index,
            detail=MAX_TURNS_ERROR,
        )
        yield Error(request_id=state.ctx.request_id, error=MAX_TURNS_ERROR)
