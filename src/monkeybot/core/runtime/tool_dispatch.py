"""Tool-batch dispatch for the text agent loop (inspectors, execute, budget append)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from collections.abc import AsyncIterator, Callable, Sequence
from typing import cast

from monkeybot.core.attachments.catalog import SessionAttachmentCatalog
from monkeybot.core.attachments.store import AttachmentStore
from monkeybot.core.context import (
    TurnContext,
    refresh_tools_after_loops_change,
    refresh_tools_after_mcp_change,
)
from monkeybot.core.context.epoch import ContextEpochTracker
from monkeybot.core.context.tool_output_policy import resolve_tool_budget
from monkeybot.core.context.tool_shapers import (
    exceeds_tool_output_budget,
    shape_tool_text,
)
from monkeybot.core.hooks import HookEvent, HookManager
from monkeybot.core.llm.provider import Provider, ToolCall
from monkeybot.core.llm.usage import Usage
from monkeybot.core.logging_utils import kv
from monkeybot.core.messages.tool_integrity import cancelled_tool_result_text
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.runtime.context_budget import ContextBudgeter
from monkeybot.core.tools.inspector import InspectorToolCall, ToolInspector
from monkeybot.core.tools.permission import remember_always_approval, resource_for_call
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.content_blocks import ContentBlock, Image, Text, ToolResponse

from .doom_loop import _doom_loop_exempt_names, _DoomLoopTracker
from .events import (
    AgentEvent,
    Error,
    ImageBlock,
    ToolCallResult,
    ToolCallStarted,
    ToolConfirmationRequestEvent,
)
from .history_compaction import HISTORY_LOAD_MAX, _append_budgeted_tool_responses
from .loop_hooks import (
    _HOOK_PRE_TOOL_TIMEOUT_S,
    _fire_hook,
    _record_tool_hook_span_event,
)
from .loop_messages import _blocks_to_sse_summary, _combine_extras, _load_agent_chat_history
from .loop_ports import ToolExecutorPort
from .loop_usage import _prompt_input_tokens_for_history
from .tool_batch import (
    _MAX_CONCURRENT_SUBAGENTS,
    _await_user_response_any,
    _chunk_tool_calls,
    _note_registry_mutation,
    _parallel_safe_names,
    _parallel_tool_concurrency,
    _rejected_tool_batch_error,
    _should_reject_tool_batch,
)

logger = logging.getLogger("monkeybot.core.runtime.loop.tool_dispatch")

_FinishTool = Callable[
    [ToolCall, ToolExecutionResult],
    tuple[ToolCallResult, ToolResponse],
]


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
        if not isinstance(b, Image):
            continue
        meta = b.metadata or {}
        path_raw = meta.get("path")
        path = path_raw.strip() if isinstance(path_raw, str) else ""
        if not path and not b.data:
            continue
        image_id = f"{call_id}:{idx}" if call_id else f"{request_id}:{idx}"
        events.append(
            ImageBlock(
                request_id=request_id,
                image_id=image_id,
                mime_type=b.mime_type,
                # Keep pixels only when there is no durable path (SSE omits data if path set).
                data="" if path else b.data,
                path=path,
            )
        )
    return events


@dataclasses.dataclass
class ToolBatchState:
    """Mutable cross-cutting state updated by :func:`dispatch_tool_batch`."""

    ctx: TurnContext
    tools_dirty: bool
    pre_tool_extra_next: str | None
    aborted: bool = False
    needs_followup_after_tools: bool = True


@dataclasses.dataclass
class _GateChunkResult:
    """Outcome of inspector/cancel gating for one tool-call chunk."""

    allowed_exec: list[ToolCall] = dataclasses.field(default_factory=list)
    chunk_responses: list[ContentBlock] = dataclasses.field(default_factory=list)
    aborted: bool = False


@dataclasses.dataclass
class _ExecuteChunkResult:
    """Outcome of serial/parallel execution for one allowed tool-call chunk."""

    mcp_mutated: bool = False
    loops_mutated: bool = False
    aborted: bool = False


async def _emit_rejected_batch(
    *,
    ordered: Sequence[ToolCall],
    stream_truncated: bool,
    request_id: str,
    thread_id: str,
    all_tool_responses: list[ContentBlock],
    finish_tool: _FinishTool,
) -> AsyncIterator[AgentEvent]:
    """Yield ToolCallStarted + error results for a truncated/incomplete batch."""
    logger.warning(
        "rejecting truncated/incomplete tool batch %s",
        kv(
            request_id=request_id,
            thread_id=thread_id,
            truncated=stream_truncated,
            n_calls=len(ordered),
        ),
    )
    for call in ordered:
        err = _rejected_tool_batch_error(call, truncated=stream_truncated)
        yield ToolCallStarted(
            request_id=request_id,
            tool=call.name,
            label=call.name,
            args=dict(call.args),
            parse_error=call.parse_error,
        )
        result_evt, tool_resp = finish_tool(call, ToolExecutionResult.err(err))
        yield result_evt
        all_tool_responses.append(tool_resp)


@dataclasses.dataclass
class _InspectorGateOutcome:
    """Allow/deny result after running inspectors for one tool call."""

    allowed: bool = True
    denial_message: str | None = None


async def _resolve_inspector_decision(
    *,
    call: ToolCall,
    ctx: TurnContext,
    inspectors: list[ToolInspector],
    outcome: _InspectorGateOutcome,
) -> AsyncIterator[AgentEvent]:
    """Run inspectors for one call; may yield ``ToolConfirmationRequestEvent``.

    Fills ``outcome`` with the final allow/deny decision. Re-raises
    ``asyncio.CancelledError`` from the confirm wait so turn abort propagates.
    """
    inspector_call = InspectorToolCall(
        call_id=call.call_id, name=call.name, args=dict(call.args)
    )
    for insp in inspectors:
        decision = await insp.check(inspector_call, ctx)
        match decision.kind:
            case "allow":
                pass
            case "deny":
                outcome.allowed = False
                outcome.denial_message = decision.message
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
                return
            case "confirm":
                if ctx.sse_bus is None:
                    outcome.allowed = False
                    outcome.denial_message = (
                        decision.message
                        or "Confirmation required but no SSE session is available"
                    )
                    return
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
                    outcome.allowed = False
                    to = int(
                        float(os.environ.get("PENDING_RESPONSE_TIMEOUT_SEC", "300"))
                    )
                    outcome.denial_message = f"user did not respond within {to}s"
                    return
                if payload.get("approved"):
                    outcome.allowed = True
                    if payload.get("always"):
                        remember_always_approval(
                            bus,
                            call.name,
                            resource_for_call(inspector_call),
                            persist=ctx.approvals_persist,
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
                    outcome.allowed = False
                    reason_raw = payload.get("reason")
                    outcome.denial_message = (
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
                return


async def _gate_chunk_calls(
    *,
    chunk: Sequence[ToolCall],
    ctx: TurnContext,
    inspectors: list[ToolInspector],
    cancelled: asyncio.Event | None,
    finish_tool: _FinishTool,
    result: _GateChunkResult,
) -> AsyncIterator[AgentEvent]:
    """Gate one chunk: cancel, parse_error, and inspector allow/deny/confirm.

    Yields ToolCallStarted/results for denied and parse_error calls, and
    ToolCallStarted for allowed calls. Fills ``result`` with allowed calls and
    chunk responses. On cancel, yields ``Error`` then sets ``result.aborted``.
    """
    allowed_exec: list[ToolCall] = []
    # Collect ToolResponse blocks for this chunk; merged into
    # ``all_tool_responses`` after every chunk completes.
    chunk_responses: list[ContentBlock] = []

    for call in chunk:
        if cancelled is not None and cancelled.is_set():
            yield Error(request_id=ctx.request_id, error="Request cancelled")
            result.allowed_exec = allowed_exec
            result.chunk_responses = chunk_responses
            result.aborted = True
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
            result_evt, tool_resp = finish_tool(
                call, ToolExecutionResult.err(call.parse_error)
            )
            yield result_evt
            chunk_responses.append(tool_resp)
            continue

        insp_outcome = _InspectorGateOutcome()
        async for evt in _resolve_inspector_decision(
            call=call,
            ctx=ctx,
            inspectors=inspectors,
            outcome=insp_outcome,
        ):
            yield evt

        if not insp_outcome.allowed:
            msg = insp_outcome.denial_message or "tool call denied"
            yield ToolCallStarted(
                request_id=ctx.request_id,
                tool=call.name,
                label=call.name,
                args=dict(call.args),
                call_id=call.call_id,
            )
            result_evt, tool_resp = finish_tool(
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

    result.allowed_exec = allowed_exec
    result.chunk_responses = chunk_responses
    result.aborted = False


async def _execute_one_tool_call(
    call: ToolCall,
    *,
    ctx: TurnContext,
    state: ToolBatchState,
    tool_executor: ToolExecutorPort,
    hook_manager: HookManager | None,
) -> ToolExecutionResult:
    """Run PRE_TOOL hooks, execute one tool, fire POST_TOOL; return the result."""
    from monkeybot.observability.spans import record_tool_outcome, span_tool

    logger.debug(
        "tool execute %s",
        kv(
            request_id=ctx.request_id,
            thread_id=ctx.thread_id,
            tool=call.name,
            call_id=call.call_id,
        ),
    )
    async with span_tool(
        tool_name=call.name,
        tool_call_id=call.call_id,
        thread_id=ctx.thread_id,
        request_id=ctx.request_id,
        args=dict(call.args),
    ):
        pre_tool_payload = await _fire_hook(
            hook_manager,
            event=HookEvent.PRE_TOOL,
            ctx=ctx,
            timeout_s=_HOOK_PRE_TOOL_TIMEOUT_S,
            tool_name=call.name,
            tool_args=dict(call.args),
        )
        if pre_tool_payload is not None and pre_tool_payload.inject_text:
            state.pre_tool_extra_next = _combine_extras(
                state.pre_tool_extra_next, pre_tool_payload.inject_text
            )
        _record_tool_hook_span_event("pre_tool", call.name)
        try:
            tool_result = await tool_executor.execute(call=call, ctx=ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "tool execution failed %s",
                kv(
                    request_id=ctx.request_id,
                    thread_id=ctx.thread_id,
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
            ctx=ctx,
            timeout_s=0,
            tool_name=call.name,
            tool_args=dict(call.args),
            tool_result=result_summary,
            tool_error=tool_result.error,
        )
        _record_tool_hook_span_event("post_tool", call.name)
    return tool_result


async def _execute_serial_chunk(
    *,
    allowed_exec: list[ToolCall],
    chunk_responses: list[ContentBlock],
    ctx: TurnContext,
    state: ToolBatchState,
    tool_executor: ToolExecutorPort,
    hook_manager: HookManager | None,
    finish_tool: _FinishTool,
    result: _ExecuteChunkResult,
) -> AsyncIterator[AgentEvent]:
    """Execute a single allowed tool call serially."""
    mcp_mutated = result.mcp_mutated
    loops_mutated = result.loops_mutated
    call = allowed_exec[0]
    try:
        tool_result = await _execute_one_tool_call(
            call,
            ctx=ctx,
            state=state,
            tool_executor=tool_executor,
            hook_manager=hook_manager,
        )
    except asyncio.CancelledError:
        yield Error(request_id=ctx.request_id, error="Request cancelled")
        result.mcp_mutated = mcp_mutated
        result.loops_mutated = loops_mutated
        result.aborted = True
        return

    event, response = finish_tool(call, tool_result)
    yield event
    for img_evt in _image_events(ctx.request_id, call.call_id, tool_result):
        yield img_evt
    chunk_responses.append(response)
    mcp_mutated, loops_mutated = _note_registry_mutation(
        call,
        tool_result,
        mcp_mutated=mcp_mutated,
        loops_mutated=loops_mutated,
    )
    result.mcp_mutated = mcp_mutated
    result.loops_mutated = loops_mutated
    result.aborted = False


async def _execute_parallel_chunk(
    *,
    allowed_exec: list[ToolCall],
    chunk_responses: list[ContentBlock],
    ctx: TurnContext,
    state: ToolBatchState,
    tool_executor: ToolExecutorPort,
    cancelled: asyncio.Event | None,
    hook_manager: HookManager | None,
    finish_tool: _FinishTool,
    result: _ExecuteChunkResult,
) -> AsyncIterator[AgentEvent]:
    """Execute multiple allowed tool calls concurrently (bounded semaphore)."""
    mcp_mutated = result.mcp_mutated
    loops_mutated = result.loops_mutated

    if cancelled is not None and cancelled.is_set():
        yield Error(request_id=ctx.request_id, error="Request cancelled")
        result.mcp_mutated = mcp_mutated
        result.loops_mutated = loops_mutated
        result.aborted = True
        return

    conc = (
        _MAX_CONCURRENT_SUBAGENTS
        if all(c.name == "task" for c in allowed_exec)
        else _parallel_tool_concurrency()
    )
    sem = asyncio.Semaphore(conc)

    async def _run_parallel(call: ToolCall) -> tuple[ToolCall, ToolExecutionResult]:
        async with sem:
            return (
                call,
                await _execute_one_tool_call(
                    call,
                    ctx=ctx,
                    state=state,
                    tool_executor=tool_executor,
                    hook_manager=hook_manager,
                ),
            )

    tasks = [asyncio.create_task(_run_parallel(c)) for c in allowed_exec]
    outcomes: list[object]
    try:
        outcomes = list(await asyncio.gather(*tasks, return_exceptions=True))
    except asyncio.CancelledError:
        for t in tasks:
            if not t.done():
                t.cancel()
        outcomes = list(await asyncio.gather(*tasks, return_exceptions=True))
        saw_cancel = True
    else:
        saw_cancel = False

    for idx, outcome in enumerate(outcomes):
        call = allowed_exec[idx]
        if isinstance(outcome, asyncio.CancelledError):
            saw_cancel = True
            continue
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

        event, response = finish_tool(call, tool_result)
        yield event
        for img_evt in _image_events(ctx.request_id, call.call_id, tool_result):
            yield img_evt
        chunk_responses.append(response)
        mcp_mutated, loops_mutated = _note_registry_mutation(
            call,
            tool_result,
            mcp_mutated=mcp_mutated,
            loops_mutated=loops_mutated,
        )

    result.mcp_mutated = mcp_mutated
    result.loops_mutated = loops_mutated
    if saw_cancel:
        yield Error(request_id=ctx.request_id, error="Request cancelled")
        result.aborted = True
        return
    result.aborted = False


async def _execute_allowed_chunk(
    *,
    allowed_exec: list[ToolCall],
    chunk_responses: list[ContentBlock],
    ctx: TurnContext,
    state: ToolBatchState,
    safe_names: frozenset[str],
    tool_executor: ToolExecutorPort,
    cancelled: asyncio.Event | None,
    hook_manager: HookManager | None,
    turn_index: int,
    finish_tool: _FinishTool,
    result: _ExecuteChunkResult,
) -> AsyncIterator[AgentEvent]:
    """Execute allowed tools for one chunk (serial or parallel).

    Yields ToolCallResult/image events and appends to ``chunk_responses``.
    Updates ``result`` with registry mutation flags; on cancel yields ``Error``
    and sets ``result.aborted``.
    """
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

    if not parallel_chunk:
        async for evt in _execute_serial_chunk(
            allowed_exec=allowed_exec,
            chunk_responses=chunk_responses,
            ctx=ctx,
            state=state,
            tool_executor=tool_executor,
            hook_manager=hook_manager,
            finish_tool=finish_tool,
            result=result,
        ):
            yield evt
    else:
        async for evt in _execute_parallel_chunk(
            allowed_exec=allowed_exec,
            chunk_responses=chunk_responses,
            ctx=ctx,
            state=state,
            tool_executor=tool_executor,
            cancelled=cancelled,
            hook_manager=hook_manager,
            finish_tool=finish_tool,
            result=result,
        ):
            yield evt


async def _post_batch_budget_and_registry(
    *,
    state: ToolBatchState,
    doom_tracker: _DoomLoopTracker,
    all_tool_responses: list[ContentBlock],
    mcp_registry_mutated: bool,
    loops_registry_mutated: bool,
    tool_executor: ToolExecutorPort,
    history: HistoryStore,
    usage: Usage,
    provider: Provider,
    pre_turn_extra: str | None,
    attachment_store: AttachmentStore | None,
    attachment_catalog: SessionAttachmentCatalog | None,
    vertex_google_search: bool,
    epoch_tracker: ContextEpochTracker,
) -> AsyncIterator[AgentEvent]:
    """Doom-loop error, budgeted history append, and MCP/loops registry refresh."""
    ctx = state.ctx
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
        # Compaction + load-max run before every provider call, so steady-state
        # history is ≤ HISTORY_LOAD_MAX. Clamp the recount input defensively for
        # any pre-compact / failed-compact window so we never tokenize an
        # unbounded migrating thread on every tool batch.
        chat_for_budget = await _load_agent_chat_history(history, ctx.thread_id)
        if len(chat_for_budget) > HISTORY_LOAD_MAX:
            logger.warning(
                "post-batch budget history exceeds load max; recounting tail only %s",
                kv(
                    request_id=ctx.request_id,
                    thread_id=ctx.thread_id,
                    message_count=len(chat_for_budget),
                    load_max=HISTORY_LOAD_MAX,
                ),
            )
            chat_for_budget = chat_for_budget[-HISTORY_LOAD_MAX:]
        budget_used = usage.estimated_prompt_tokens
        try:
            budget_used = await _prompt_input_tokens_for_history(
                ctx=ctx,
                chat_messages=chat_for_budget,
                provider=provider,
                attachment_store=attachment_store,
                attachment_catalog=attachment_catalog,
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
        budgeter = ContextBudgeter.for_window(
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
        state.ctx = ctx
        state.tools_dirty = True
        doom_tracker.exempt_names = _doom_loop_exempt_names(ctx.tools)
        logger.info(
            "refreshed ctx.tools after MCP registry change %s",
            kv(
                request_id=ctx.request_id,
                thread_id=ctx.thread_id,
                tool_count=len(ctx.tools),
            ),
        )

    if loops_registry_mutated:
        advertised = bool(getattr(tool_executor, "loops_advertised", False))
        ctx = refresh_tools_after_loops_change(ctx, loops_advertised=advertised)
        state.ctx = ctx
        state.tools_dirty = True
        doom_tracker.exempt_names = _doom_loop_exempt_names(ctx.tools)
        logger.info(
            "refreshed ctx.tools after loops registry change %s",
            kv(
                request_id=ctx.request_id,
                thread_id=ctx.thread_id,
                loops_advertised=advertised,
                tool_count=len(ctx.tools),
            ),
        )


def _fill_abort_tool_responses(
    *,
    ordered: Sequence[ToolCall],
    all_tool_responses: list[ContentBlock],
    pending_chunk_responses: Sequence[ContentBlock] | None,
    finish_tool: _FinishTool,
    request_id: str,
    thread_id: str,
) -> list[ToolCallResult]:
    """Merge pending chunk results; synthesize cancel envelopes for missing ids.

    Mutates ``all_tool_responses`` in place. Returns SSE events for newly
    synthesized cancel results (completed tools already emitted theirs).
    """
    if pending_chunk_responses:
        all_tool_responses.extend(pending_chunk_responses)

    responded_ids = {
        block.id
        for block in all_tool_responses
        if isinstance(block, ToolResponse)
    }
    completed_count = len(responded_ids)
    events: list[ToolCallResult] = []
    for call in ordered:
        if call.call_id in responded_ids:
            continue
        result_evt, tool_resp = finish_tool(
            call, ToolExecutionResult.err(cancelled_tool_result_text(call.name))
        )
        events.append(result_evt)
        all_tool_responses.append(tool_resp)
        responded_ids.add(call.call_id)

    logger.info(
        "tool batch abort settle %s",
        kv(
            request_id=request_id,
            thread_id=thread_id,
            completed_count=completed_count,
            cancelled_count=len(events),
        ),
    )
    return events


async def dispatch_tool_batch(
    *,
    ordered: Sequence[ToolCall],
    stream_truncated: bool,
    state: ToolBatchState,
    doom_tracker: _DoomLoopTracker,
    inspectors: list[ToolInspector],
    tool_executor: ToolExecutorPort,
    cancelled: asyncio.Event | None,
    history: HistoryStore,
    usage: Usage,
    provider: Provider,
    hook_manager: HookManager | None,
    turn_index: int,
    pre_turn_extra: str | None,
    attachment_store: AttachmentStore | None,
    attachment_catalog: SessionAttachmentCatalog | None,
    vertex_google_search: bool,
    epoch_tracker: ContextEpochTracker,
) -> AsyncIterator[AgentEvent]:
    """Execute one provider tool-call batch; yield events; update ``state`` in place.

    On cancellation mid-batch, settles completed results into history, synthesizes
    cancel envelopes only for unrecorded call ids, sets ``state.aborted``, and
    returns so the caller can ``return`` from the outer turn loop.
    """
    ctx = state.ctx

    def _finish_tool(
        call: ToolCall,
        result: ToolExecutionResult,
    ) -> tuple[ToolCallResult, ToolResponse]:
        event, response = _tool_outcome(call, ctx.request_id, result)
        doom_tracker.record(call.name, dict(call.args))
        return event, response

    # One user Message for the whole model tool-call turn (all chunks).
    # Gemini 400s when functionResponse count != functionCall count on the
    # preceding model turn; serial non-task tools are separate chunks but
    # must still share a single user row.
    all_tool_responses: list[ContentBlock] = []
    mcp_registry_mutated = False
    loops_registry_mutated = False
    abort_pending: Sequence[ContentBlock] | None = None
    # Computed once per batch and reused for both chunking and dispatch
    # so the two stay consistent even if ctx.tools were ever mutated
    # mid-batch (e.g. by an MCP registry reload).
    safe_names = _parallel_safe_names(ctx.tools)
    reject_batch = _should_reject_tool_batch(ordered, truncated=stream_truncated)
    if reject_batch:
        async for evt in _emit_rejected_batch(
            ordered=ordered,
            stream_truncated=stream_truncated,
            request_id=ctx.request_id,
            thread_id=ctx.thread_id,
            all_tool_responses=all_tool_responses,
            finish_tool=_finish_tool,
        ):
            yield evt

    for chunk in (() if reject_batch else _chunk_tool_calls(
        ordered, parallel_safe=safe_names
    )):
        if cancelled is not None and cancelled.is_set():
            yield Error(request_id=ctx.request_id, error="Request cancelled")
            state.aborted = True
            state.needs_followup_after_tools = False
            abort_pending = None
            break

        gate_result = _GateChunkResult()
        async for evt in _gate_chunk_calls(
            chunk=chunk,
            ctx=ctx,
            inspectors=inspectors,
            cancelled=cancelled,
            finish_tool=_finish_tool,
            result=gate_result,
        ):
            yield evt

        if gate_result.aborted:
            state.aborted = True
            state.needs_followup_after_tools = False
            abort_pending = gate_result.chunk_responses
            break

        allowed_exec = gate_result.allowed_exec
        chunk_responses = gate_result.chunk_responses

        if not allowed_exec:
            all_tool_responses.extend(chunk_responses)
            continue

        exec_result = _ExecuteChunkResult(
            mcp_mutated=mcp_registry_mutated,
            loops_mutated=loops_registry_mutated,
        )
        async for evt in _execute_allowed_chunk(
            allowed_exec=allowed_exec,
            chunk_responses=chunk_responses,
            ctx=ctx,
            state=state,
            safe_names=safe_names,
            tool_executor=tool_executor,
            cancelled=cancelled,
            hook_manager=hook_manager,
            turn_index=turn_index,
            finish_tool=_finish_tool,
            result=exec_result,
        ):
            yield evt

        mcp_registry_mutated = exec_result.mcp_mutated
        loops_registry_mutated = exec_result.loops_mutated
        if exec_result.aborted:
            state.aborted = True
            state.needs_followup_after_tools = False
            abort_pending = chunk_responses
            break

        all_tool_responses.extend(chunk_responses)

    if state.aborted:
        for evt in _fill_abort_tool_responses(
            ordered=ordered,
            all_tool_responses=all_tool_responses,
            pending_chunk_responses=abort_pending,
            finish_tool=_finish_tool,
            request_id=ctx.request_id,
            thread_id=ctx.thread_id,
        ):
            yield evt

    async for evt in _post_batch_budget_and_registry(
        state=state,
        doom_tracker=doom_tracker,
        all_tool_responses=all_tool_responses,
        mcp_registry_mutated=mcp_registry_mutated,
        loops_registry_mutated=loops_registry_mutated,
        tool_executor=tool_executor,
        history=history,
        usage=usage,
        provider=provider,
        pre_turn_extra=pre_turn_extra,
        attachment_store=attachment_store,
        attachment_catalog=attachment_catalog,
        vertex_google_search=vertex_google_search,
        epoch_tracker=epoch_tracker,
    ):
        yield evt
