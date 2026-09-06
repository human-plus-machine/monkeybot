"""Inner turn loop: prepare → compact → stream → empty/final or tool dispatch."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Sequence
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
from monkeybot.core.hooks import HookEvent, HookManager
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
from monkeybot.core.memory.ingest import persist_message
from monkeybot.core.messages import convert_to_provider
from monkeybot.core.messages.tool_integrity import (
    cancelled_tool_result_text,
    interrupted_tool_result_text,
)
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.persistence.transcript import TranscriptWriter
from monkeybot.core.prompts.prompt import latest_user_message_text
from monkeybot.core.runtime.context_budget import (
    SUMMARY_TRIGGER_RATIO,
    compute_context_pressure_tier,
)
from monkeybot.core.runtime.provider_stream_mapper import ProviderStreamMapper
from monkeybot.core.tools.inspector import ToolInspector
from monkeybot.core.types.content_blocks import (
    ContentBlock,
    Text,
    ToolRequest,
    ToolResponse,
)
from monkeybot.core.types.content_blocks import Thinking as ThinkingBlock
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._utils import note_anthropic_token_estimate_observation
from monkeybot.providers.pricing import estimate_cost

from .doom_loop import (
    _doom_loop_exempt_names,
    _DoomLoopTracker,
    _effective_doom_loop_threshold,
)
from .events import (
    AgentEvent,
    AttachmentDescriptorEvent,
    ContextSummarized,
    ContextSummarizing,
    ContextUsage,
    Error,
    SystemPromptSnapshot,
    Thinking,
    UserSteered,
)
from .history_compaction import (
    HISTORY_LOAD_MAX,
    _await_history_write,
    _intent_facts_from_ctx,
    _summarization_model_id,
    _summarization_viable,
    _summarize_history,
    protect_recent_count,
)
from .input_admission import InputAdmission, SteerItem, join_text, preview_text
from .loop_hooks import (
    _HOOK_READ_TIMEOUT_S,
    _apply_before_provider_hook,
    _drain_hook_settlement,
    _fire_after_provider_response,
    _fire_hook,
)
from .loop_messages import (
    _admit_system_context,
    _append_extra_system_text,
    _combine_extras,
    _epoch_events,
    _load_agent_chat_history,
    _messages_for_provider,
    _provider_messages_prompt_summary,
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
from .tool_dispatch import ToolBatchState, dispatch_tool_batch

logger = logging.getLogger("monkeybot.core.runtime.loop.turn_loop")

# Emitted as an ``Error`` event when a run hits its turn budget. The parent task
# tool matches on this exact value to report ``exit_reason="max_turns"``, so it is
# a contract between the loop and the subagent completion payload, not a message.
MAX_TURNS_ERROR = "Max turns exceeded"

TurnAction = Literal["continue", "break", "return"]

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


def _require_prompt(state: _TurnState) -> tuple[Message, EpochAdmit]:
    if state.system is None or state.admit is None:
        raise RuntimeError("turn prepare did not set system/admit")
    return state.system, state.admit


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
                logger.debug(
                    "observability trace id unavailable %s",
                    kv(request_id=ctx.request_id, thread_id=ctx.thread_id),
                    exc_info=True,
                )


async def _drain_steers(
    *,
    input_admission: InputAdmission | None,
    history: HistoryStore,
    ctx: TurnContext,
) -> AsyncIterator[AgentEvent]:
    """Inject queued steer messages at a safe boundary (before next provider call)."""
    if input_admission is None:
        return
    while True:
        item = input_admission.pop_steer()
        if item is None:
            break
        steered = item.content
        await persist_message(
            history,
            Message(role="user", content=list(steered)),
            thread_id=ctx.thread_id,
            turn_id=ctx.request_id,
            memory=ctx.memory,
            ingest=True,
        )
        preview = preview_text(steered)
        _admit_steer_to_ledger(ctx, join_text(steered), item)
        logger.info(
            "steer injected %s",
            kv(
                request_id=ctx.request_id,
                thread_id=ctx.thread_id,
                preview=preview[:80],
                provenance=item.provenance,
            ),
        )
        yield UserSteered(request_id=ctx.request_id, text=preview)


def _admit_steer_to_ledger(ctx: TurnContext, verbatim: str, item: SteerItem) -> None:
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
    tools_dirty: bool = False
    assistant_write_task: asyncio.Task[None] | None = None
    pre_turn_extra: str | None = None
    pre_tool_extra_next: str | None = None
    empty_completion_retries_left: int = _EMPTY_COMPLETION_RETRIES
    post_tool_empty_retries_left: int = _POST_TOOL_EMPTY_RETRIES
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
    pending: dict[str, ToolCall] = dataclasses.field(default_factory=dict)
    assistant_text: str = ""
    thinking_text: str = ""
    thinking_signature: str | None = None
    stream_truncated: bool = False
    last_preflight_tokens: int = 0
    provider_stream_failed: bool = False
    tools_dirty_reason: str | None = None
    tools_schema_hash: str = ""


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
    force_no_tools, doom_loop_note = state.doom_tracker.consume_recovery()
    combined_extra = _combine_extras(combined_extra, doom_loop_note)
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
        session_id=state.ctx.thread_id,
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
        state.pre_turn_extra,
    )
    state.resolved_messages = convert_to_provider(
        state.chat_messages,
        attachment_store=attachment_store,
        session_id=state.ctx.thread_id,
    )
    system_msg, admit = _require_prompt(state)
    state.provider_messages = _messages_for_provider(
        system_msg,
        state.resolved_messages,
        mid_conversation_update=admit.mid_conversation_update,
    )


def _token_pressure_should_summarize(
    chat_messages: list[Message],
    *,
    preflight: int,
    cap: int,
    window_tokens: int,
) -> bool:
    return preflight >= cap and _summarization_viable(chat_messages, window_tokens=window_tokens)


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
            turns_summarized = await _summarize_history(
                state.ctx.thread_id,
                state.chat_messages,
                history,
                provider,
                _summarization_model_id(state.ctx),
                window_tokens=state.ctx.context_window_tokens,
                intent_facts=_intent_facts_from_ctx(state.ctx),
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
) -> AsyncIterator[AgentEvent]:
    """Compact when prompt tokens hit the context-window ratio threshold."""
    if not _token_pressure_should_summarize(
        state.chat_messages,
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
    dropped = len(state.chat_messages) - HISTORY_LOAD_MAX
    truncated = state.chat_messages[-HISTORY_LOAD_MAX:]
    logger.error(
        "history exceeds load max after compact attempt; truncating tail %s",
        kv(
            request_id=state.ctx.request_id,
            thread_id=state.ctx.thread_id,
            message_count=len(state.chat_messages),
            load_max=HISTORY_LOAD_MAX,
            dropped=dropped,
        ),
    )
    await history.reset(state.ctx.thread_id, truncated)
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
            session_id=state.ctx.thread_id,
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
        text=_system_prompt_snapshot_text(system_msg, admit.mid_conversation_update),
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


async def _consume_provider_stream_body(
    state: _TurnState,
    *,
    provider: Provider,
    usage: Usage,
    hook_manager: HookManager | None,
    transcript_writer: TranscriptWriter | None,
    vertex_google_search: bool,
    stream_mapper: ProviderStreamMapper,
    cancelled: asyncio.Event | None = None,
) -> AsyncIterator[AgentEvent]:
    """Consume provider stream, write response transcript, fire after-response hook.

    Sets ``state.action = "return"`` on cancel or provider error.
    """
    from monkeybot.observability.spans import set_llm_io, set_llm_usage, span_llm

    llm_input = 0
    llm_output = 0
    llm_cached = 0
    llm_cache_read = 0
    llm_cache_creation = 0
    hints = _provider_call_hints(state.ctx)
    try:
        async with span_llm(ctx=state.ctx, vertex_google_search=vertex_google_search):
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
                async for ev in stream:
                    # Stop mid-token-stream when POST /cancel sets the event —
                    # otherwise Stop only takes effect after the full LLM call.
                    if cancelled is not None and cancelled.is_set():
                        raise asyncio.CancelledError
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
            state.pending = stream_mapper.pending
            state.assistant_text = stream_mapper.assistant_text
            state.thinking_text = stream_mapper.thinking_text
            state.thinking_signature = stream_mapper.thinking_signature
            state.stream_truncated = stream_mapper.stream_truncated
            set_llm_usage(
                input_tokens=llm_input,
                output_tokens=llm_output,
                cached_tokens=llm_cached,
                cache_read_tokens=llm_cache_read,
                cache_creation_tokens=llm_cache_creation,
            )
            set_llm_io(
                prompt=_provider_messages_prompt_summary(state.provider_messages),
                completion=state.assistant_text or "",
            )
            actual_prompt = llm_input + llm_cache_read + llm_cache_creation
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
                request_id=state.ctx.request_id,
                thread_id=state.ctx.thread_id,
                turn=state.turn_index,
                llm_input=llm_input,
                llm_output=llm_output,
                llm_cached=llm_cached,
                tool_calls=len(state.pending),
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
                usage={
                    "input_tokens": llm_input,
                    "output_tokens": llm_output,
                    "cached_tokens": llm_cached,
                    "cache_read_tokens": llm_cache_read,
                    "cache_creation_tokens": llm_cache_creation,
                },
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
    except Exception as exc:
        for aev in stream_mapper.finish():
            yield aev
        _sync_stream_mapper_text(state, stream_mapper)
        logger.exception(
            "provider stream failed %s",
            kv(
                request_id=state.ctx.request_id,
                thread_id=state.ctx.thread_id,
                model=state.ctx.model,
                partial_chars=len(state.assistant_text or ""),
            ),
        )
        await _fire_after_provider_response(
            hook_manager,
            ctx=state.ctx,
            inner_turn=state.turn_index,
            assistant_text=state.assistant_text,
            thinking_text=state.thinking_text,
            provider_error=str(exc),
        )
        yield Error(request_id=state.ctx.request_id, error=str(exc))
        state.needs_followup_after_tools = False
        state.provider_stream_failed = True
        state.action = "return"
        return


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
    tool_text_fn = (
        interrupted_tool_result_text if state.provider_stream_failed else cancelled_tool_result_text
    )
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


async def _stream_provider_turn(
    state: _TurnState,
    *,
    provider: Provider,
    usage: Usage,
    hook_manager: HookManager | None,
    transcript_writer: TranscriptWriter | None,
    vertex_google_search: bool,
    cancelled: asyncio.Event | None = None,
) -> AsyncIterator[AgentEvent]:
    """Stream provider events; fill ``state.pending`` / text fields. May set action=return."""
    await _write_transcript_provider_request(state, transcript_writer=transcript_writer)
    stream_mapper = ProviderStreamMapper(state.ctx.request_id)
    async for evt in _consume_provider_stream_body(
        state,
        provider=provider,
        usage=usage,
        hook_manager=hook_manager,
        transcript_writer=transcript_writer,
        vertex_google_search=vertex_google_search,
        stream_mapper=stream_mapper,
        cancelled=cancelled,
    ):
        yield evt


async def _handle_empty_or_final_text(
    state: _TurnState,
    *,
    history: HistoryStore,
    last_assistant: list[str],
) -> AsyncIterator[AgentEvent]:
    """No tool calls: final assistant write, empty-completion retry, or exhausted Error."""
    cleaned_text = (state.assistant_text or "").strip()
    if cleaned_text:
        # Fire-and-forget: this is the final assistant turn (we break
        # right after), so nothing reads it again in-loop. Keeping it
        # off the await path means the Firestore write does not delay
        # TurnComplete / the user-visible reply. Awaited at the tail.
        assist_blocks: list[ContentBlock] = []
        thinking = _thinking_block(state)
        if thinking is not None:
            assist_blocks.append(thinking)
        assist_blocks.append(Text(text=cleaned_text))
        state.assistant_write_task = asyncio.create_task(
            persist_message(
                history,
                Message(role="assistant", content=assist_blocks),
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
        logger.warning(log_msg, kv(**log_fields))
        state.action = "continue"
        return
    logger.warning("empty model completion; ending turn %s", kv(**log_fields))
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
    await persist_message(
        history,
        Message(role="user", content=list(user_content)),
        thread_id=state.ctx.thread_id,
        turn_id=state.ctx.request_id,
        memory=state.ctx.memory,
        ingest=True,
    )

    await _fire_hook(
        hook_manager,
        event=HookEvent.USER_MESSAGE,
        ctx=state.ctx,
        timeout_s=0,
        user_message=state.user_text,
    )

    while state.turn_index < state.effective_max:
        if cancelled is not None and cancelled.is_set():
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

        state.turn_index += 1
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
                cancelled=cancelled,
            ):
                yield evt
            if state.action == "return":
                # Cancel/error mid-stream skips _handle_empty_or_final_text —
                # persist any text already shown to the user before exiting.
                await _persist_partial_assistant_on_abort(
                    state, history=history, last_assistant=last_assistant
                )
                return
            state.action = None

            if cancelled is not None and cancelled.is_set():
                # Stream already finished (and may have been shown over SSE).
                # Persist text and settle any finalized tool calls the same way
                # mid-stream abort does.
                yield Error(request_id=state.ctx.request_id, error="Request cancelled")
                state.needs_followup_after_tools = False
                await _persist_partial_assistant_on_abort(
                    state, history=history, last_assistant=last_assistant
                )
                break

            if not state.pending:
                async for evt in _handle_empty_or_final_text(
                    state, history=history, last_assistant=last_assistant
                ):
                    yield evt
                if state.action == "continue":
                    continue
                if state.action == "break":
                    break

            else:
                async for evt in _append_tool_requests_and_dispatch(
                    state,
                    history=history,
                    inspectors=inspectors,
                    tool_executor=tool_executor,
                    cancelled=cancelled,
                    usage=usage,
                    provider=provider,
                    hook_manager=hook_manager,
                    attachment_store=attachment_store,
                    attachment_catalog=attachment_catalog,
                    vertex_google_search=vertex_google_search,
                ):
                    yield evt
                if state.action == "return":
                    return

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
        yield Error(request_id=state.ctx.request_id, error=MAX_TURNS_ERROR)

    # Ensure the backgrounded assistant write has landed before any load/reset
    # below (freeze) so the assistant row is durable and not overwritten.
    await _await_history_write(state.assistant_write_task)

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
