"""Post-utterance processing for the realtime harness.

This module mirrors :func:`~monkeybot.core.runtime.loop.run` for realtime turns: it
commits finalized user/assistant utterances to ``HistoryStore``, runs hooks, and
dispatches tools. It does **not** call a model provider; the realtime vendor session
is managed by the gateway.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.attachments.catalog import SessionAttachmentCatalog
from monkeybot.core.attachments.freeze import freeze_attachments_in_history
from monkeybot.core.attachments.store import AttachmentStore
from monkeybot.core.context import (
    PendingResponseBusPort,
    TurnContext,
    refresh_memory_index,
    refresh_tools_after_loops_change,
    refresh_tools_after_mcp_change,
)
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.memory.ingest import persist_message
from monkeybot.core.llm.provider import Message, ToolCall
from monkeybot.core.llm.realtime_provider import RealtimeToolCall
from monkeybot.core.logging_utils import kv
from monkeybot.core.messages.tool_integrity import cancelled_tool_result_text
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.persistence.transcript import TranscriptWriter
from monkeybot.core.runtime.events import (
    ActionRequiredEvent,
    AgentEvent,
    AssistantDelta,
    AssistantTextEnded,
    AttachmentDescriptorEvent,
    Error,
    Thinking,
    ToolCallResult,
    ToolCallStarted,
    ToolConfirmationRequestEvent,
    TurnComplete,
    UsageTotals,
)
from monkeybot.core.tools.inspector import InspectorToolCall
from monkeybot.core.tools.permission import remember_always_approval, resource_for_call
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.content_blocks import (
    ActionRequired,
    ContentBlock,
    ElicitationAction,
    Text,
    ToolRequest,
    ToolResponse,
)

from .loop_ports import ToolExecutorPort
from .tool_batch import (
    _await_user_response_any,
    _note_registry_mutation,
    _rejected_tool_batch_error,
    _should_reject_tool_batch,
    confirm_wait_stopped_by_user,
    uncancel_current_task,
)

logger = logging.getLogger("monkeybot.core.runtime.realtime_loop")


_HOOK_PRE_TOOL_TIMEOUT_S = 1.5

_REALTIME_MCP_NEW_SESSION_NOTE = (
    "MCP registry updated on the harness. Newly enabled MCP tool schemas are not "
    "pushed into the live voice session; start a new realtime session to advertise "
    "them to the model (v1 has no reconnect/resume)."
)

_REALTIME_LOOPS_NEW_SESSION_NOTE = (
    "Scheduled-loop tool advertisement updated on the harness. Newly enabled loop "
    "tool schemas are not pushed into the live voice session; start a new realtime "
    "session to advertise them to the model (v1 has no reconnect/resume)."
)


def _user_text_from_content(blocks: Sequence[ContentBlock]) -> str:
    return " ".join(
        b.text.strip() for b in blocks if isinstance(b, Text) and b.text.strip()
    )


def _elicitation_user_data_text(user_data: object) -> str:
    if user_data is None:
        return ""
    if isinstance(user_data, str):
        return user_data
    try:
        return json.dumps(user_data, sort_keys=True)
    except (TypeError, ValueError):
        return str(user_data)


async def _resolve_elicitation_blocks(
    blocks: Sequence[ContentBlock],
    *,
    ctx: TurnContext,
    pending_bus: PendingResponseBusPort | None,
) -> AsyncIterator[AgentEvent | list[ContentBlock]]:
    """Yield ``ActionRequiredEvent``s for elicitation blocks, then the resolved block list.

    Tools may return :class:`ActionRequired` / :class:`ElicitationAction` blocks to pause
    the realtime turn until the client answers via ``elicitation_response``.
    """
    out: list[ContentBlock] = []
    for block in blocks:
        if not (
            isinstance(block, ActionRequired) and isinstance(block.data, ElicitationAction)
        ):
            out.append(block)
            continue
        action = block.data
        elicitation_id = action.id.strip() or str(uuid.uuid4())
        if pending_bus is None:
            logger.warning(
                "realtime elicitation unavailable %s",
                kv(
                    request_id=ctx.request_id,
                    thread_id=ctx.thread_id,
                    elicitation_id=elicitation_id,
                ),
            )
            out.append(
                Text(text=f"{action.message} (elicitation unavailable)")
            )
            continue
        fut = pending_bus.register_pending(elicitation_id)
        yield ActionRequiredEvent(
            request_id=ctx.request_id,
            action_type="elicitation",
            id=elicitation_id,
            payload={
                "message": action.message,
                "requestedSchema": dict(action.requested_schema or {}),
            },
        )
        try:
            payload = await _await_user_response_any(
                pending_bus, fut, elicitation_id, timeout_sec=None
            )
        except asyncio.CancelledError:
            raise
        if payload.get("_timeout"):
            out.append(Text(text="User did not respond to elicitation in time"))
            continue
        if payload.get("cancelled"):
            out.append(Text(text="User cancelled elicitation"))
            continue
        out.append(Text(text=_elicitation_user_data_text(payload.get("user_data"))))
    yield out


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
) -> HookPayload | None:
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
    )
    return await hook_manager.fire(payload, timeout_s=timeout_s)


def _blocks_to_sse_summary(blocks: Sequence[ContentBlock]) -> str:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, Text):
            parts.append(block.text)
        else:
            parts.append(f"[{type(block).__name__}]")
    return "\n".join(parts)


def _tool_outcome(
    call: ToolCall,
    request_id: str,
    result: ToolExecutionResult,
) -> tuple[ToolCallResult, ToolResponse]:
    is_error = result.error is not None
    body = "" if is_error else _blocks_to_sse_summary(result.blocks)
    text = result.error if is_error else body
    response_blocks: list[ContentBlock] = (
        list(result.blocks) if not is_error else [Text(text=text or "")]
    )
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


@dataclasses.dataclass
class _RealtimeInspectorGate:
    """Outcome of realtime inspector / HITL gating for one tool call."""

    allowed: bool = True
    denial_message: str | None = None
    aborted: bool = False
    settled_responses: list[ToolResponse] = dataclasses.field(default_factory=list)


async def _resolve_realtime_inspector_decision(
    *,
    call: ToolCall,
    call_index: int,
    pending_calls: Sequence[ToolCall],
    ctx: TurnContext,
    inspectors: Sequence[Any] | None,
    pending_bus: PendingResponseBusPort | None,
    outcome: _RealtimeInspectorGate,
) -> AsyncIterator[AgentEvent]:
    """Run inspectors for one call; may yield ``ToolConfirmationRequestEvent``.

    On confirm cancel, yields cancel envelopes for this call and any remaining
    tools, sets ``outcome.aborted``, and returns. Otherwise fills ``outcome``
    with the allow/deny decision.
    """
    inspector_call = InspectorToolCall(
        call_id=call.call_id, name=call.name, args=dict(call.args)
    )
    for insp in inspectors or []:
        decision = await insp.check(inspector_call, ctx)
        if decision.kind == "deny":
            outcome.allowed = False
            outcome.denial_message = decision.message
            return
        if decision.kind != "confirm":
            continue
        if pending_bus is None:
            outcome.allowed = False
            outcome.denial_message = (
                decision.message
                or "Confirmation required but realtime HITL is unavailable"
            )
            logger.warning(
                "realtime HITL unavailable %s",
                kv(
                    request_id=ctx.request_id,
                    thread_id=ctx.thread_id,
                    call_id=call.call_id,
                    tool=call.name,
                ),
            )
            return
        fut = pending_bus.register_pending(call.call_id)
        yield ToolConfirmationRequestEvent(
            request_id=ctx.request_id,
            tool_call_id=call.call_id,
            tool_name=call.name,
            arguments=dict(call.args),
            prompt=decision.message,
        )
        try:
            payload = await _await_user_response_any(
                pending_bus, fut, call.call_id, timeout_sec=None
            )
        except asyncio.CancelledError:
            if not confirm_wait_stopped_by_user(pending_bus, call.call_id):
                uncancel_current_task()
                raise
            yield Error(request_id=ctx.request_id, error="Request cancelled")
            remaining = pending_calls[call_index:]
            logger.info(
                "realtime HITL cancelled %s",
                kv(
                    request_id=ctx.request_id,
                    thread_id=ctx.thread_id,
                    call_id=call.call_id,
                    approved=False,
                    reason="cancelled",
                    cancelled_count=len(remaining),
                ),
            )
            outcome.aborted = True
            for pending in remaining:
                event, response = _tool_outcome(
                    pending,
                    ctx.request_id,
                    ToolExecutionResult.err(cancelled_tool_result_text(pending.name)),
                )
                yield event
                outcome.settled_responses.append(response)
            return
        if payload.get("_timeout"):
            outcome.allowed = False
            outcome.denial_message = "user did not respond in time"
            logger.info(
                "realtime HITL timeout %s",
                kv(
                    request_id=ctx.request_id,
                    thread_id=ctx.thread_id,
                    call_id=call.call_id,
                    approved=False,
                    reason="timeout",
                ),
            )
            return
        if payload.get("approved"):
            outcome.allowed = True
            if payload.get("always"):
                remember_always_approval(
                    pending_bus, call.name, resource_for_call(inspector_call)
                )
                logger.debug(
                    "realtime HITL always %s",
                    kv(
                        request_id=ctx.request_id,
                        thread_id=ctx.thread_id,
                        call_id=call.call_id,
                        tool=call.name,
                        decision="confirm_always",
                    ),
                )
            return
        outcome.allowed = False
        reason_raw = payload.get("reason")
        outcome.denial_message = (
            (reason_raw if isinstance(reason_raw, str) else None)
            or decision.message
            or "denied by user"
        )
        logger.info(
            "realtime HITL denied %s",
            kv(
                request_id=ctx.request_id,
                thread_id=ctx.thread_id,
                call_id=call.call_id,
                approved=False,
                reason=outcome.denial_message,
            ),
        )
        return


async def _execute_tool(
    call: ToolCall,
    ctx: TurnContext,
    *,
    tool_executor: ToolExecutorPort,
    hook_manager: HookManager | None,
    request_id: str,
) -> tuple[ToolExecutionResult, str | None]:
    """Execute one tool; return (result, optional inject_text from pre-tool hooks)."""
    logger.debug(
        "realtime tool execute %s",
        kv(
            request_id=request_id,
            thread_id=ctx.thread_id,
            tool=call.name,
            call_id=call.call_id,
        ),
    )
    pre_tool_payload = await _fire_hook(
        hook_manager,
        event=HookEvent.PRE_TOOL,
        ctx=ctx,
        timeout_s=_HOOK_PRE_TOOL_TIMEOUT_S,
        tool_name=call.name,
        tool_args=dict(call.args),
    )
    inject_text: str | None = None
    if pre_tool_payload is not None and pre_tool_payload.inject_text:
        inject_text = pre_tool_payload.inject_text
    try:
        result = await tool_executor.execute(call=call, ctx=ctx)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "realtime tool execution failed %s",
            kv(
                request_id=request_id,
                thread_id=ctx.thread_id,
                tool=call.name,
                call_id=call.call_id,
            ),
            exc_info=True,
        )
        result = ToolExecutionResult.err(str(exc))
    result_summary = (
        _blocks_to_sse_summary(result.blocks) if result.error is None else None
    )
    await _fire_hook(
        hook_manager,
        event=HookEvent.POST_TOOL,
        ctx=ctx,
        timeout_s=0,
        tool_name=call.name,
        tool_args=dict(call.args),
        tool_result=result_summary,
        tool_error=result.error,
    )
    return result, inject_text


def _realtime_tool_call_to_tool_call(call: RealtimeToolCall) -> ToolCall:
    return ToolCall(
        call_id=call.call_id,
        name=call.name,
        args=dict(call.args),
        parse_error=call.parse_error,
    )


async def run_realtime_turn(
    user_content: str | list[ContentBlock],
    assistant_text: str,
    assistant_tool_calls: Sequence[RealtimeToolCall],
    ctx: TurnContext,
    *,
    history: HistoryStore,
    tool_executor: ToolExecutorPort,
    inspectors: Sequence[Any] | None = None,
    hook_manager: HookManager | None = None,
    attachment_store: AttachmentStore | None = None,
    attachment_catalog: SessionAttachmentCatalog | None = None,
    transcript_writer: TranscriptWriter | None = None,
    tool_results_out: list[ContentBlock] | None = None,
    inject_texts_out: list[str] | None = None,
    pending_bus: PendingResponseBusPort | None = None,
) -> AsyncIterator[AgentEvent]:
    """Process a finalized realtime user utterance + assistant response.

    Commits the user message, runs memory hooks, commits the assistant message (with
    any tool requests), and dispatches the tools sequentially. Tool results are
    yielded as ``ToolCallResult`` events and also appended to ``tool_results_out``
    when the generator completes. Pre-tool hook ``inject_text`` values are appended to
    ``inject_texts_out`` for the gateway to inject into the live session.

    The caller is responsible for injecting the collected tool results back into the
    live realtime session via ``RealtimeSession.send_tool_results()`` /
    ``send_context()`` when the session is idle.

    Truncation (``Done.truncated``) is text-loop only — Gemini Live does not expose an
    output length-limit signal. Realtime still rejects all-``parse_error`` batches.
    """
    usage = UsageTotals()
    # attachment_store is accepted for symmetry with loop.run and future resolve paths;
    # freeze_attachments_in_history uses the catalog rebuilt from history.
    _ = attachment_store
    blocks = [Text(text=user_content)] if isinstance(user_content, str) else list(user_content)
    user_text = _user_text_from_content(blocks)

    logger.debug(
        "realtime turn start %s",
        kv(
            request_id=ctx.request_id,
            thread_id=ctx.thread_id,
            model=ctx.model,
            user_text_len=len(user_text),
            n_tool_calls=len(assistant_tool_calls),
        ),
    )

    try:
        # 1. Commit user message to history (skip empty audio-only placeholders).
        if user_text or any(not isinstance(b, Text) for b in blocks):
            await persist_message(
                history,
                Message(role="user", content=list(blocks)),
                thread_id=ctx.thread_id,
                turn_id=ctx.request_id,
                memory=ctx.memory,
                ingest=True,
            )
            await _fire_hook(
                hook_manager,
                event=HookEvent.USER_MESSAGE,
                ctx=ctx,
                timeout_s=0,
                user_message=user_text,
            )

            if transcript_writer is not None:
                await transcript_writer.write_user_message(
                    request_id=ctx.request_id,
                    content=user_text,
                )

            # 2. Refresh memory index for the next system prompt.
            ctx = await refresh_memory_index(ctx)

        yield Thinking(request_id=ctx.request_id)

        # 3. Build assistant message with text + tool requests.
        assistant_blocks: list[ContentBlock] = []
        if assistant_text.strip():
            assistant_blocks.append(Text(text=assistant_text.strip()))
            yield AssistantDelta(request_id=ctx.request_id, delta=assistant_text.strip())
            yield AssistantTextEnded(request_id=ctx.request_id, text=assistant_text.strip())

        tool_results: list[ContentBlock] = []
        for rtc in assistant_tool_calls:
            assistant_blocks.append(
                ToolRequest(
                    id=rtc.call_id,
                    name=rtc.name,
                    args=dict(rtc.args),
                    parse_error=rtc.parse_error,
                )
            )

        if assistant_blocks:
            # Await the assistant write so tool-response rows are always ordered after it.
            await persist_message(
                history,
                Message(role="assistant", content=assistant_blocks),
                thread_id=ctx.thread_id,
                turn_id=ctx.request_id,
                memory=ctx.memory,
                ingest=bool(assistant_text.strip()) and not assistant_tool_calls,
            )

        # 4. Dispatch tools sequentially (v1: no parallel subagent dispatch here).
        mcp_registry_mutated = False
        loops_registry_mutated = False
        pending_calls = [_realtime_tool_call_to_tool_call(rtc) for rtc in assistant_tool_calls]
        # Realtime has no vendor length-limit signal; reject only all-parse_error batches.
        reject_batch = _should_reject_tool_batch(pending_calls, truncated=False)
        if reject_batch:
            logger.warning(
                "rejecting incomplete realtime tool batch %s",
                kv(
                    request_id=ctx.request_id,
                    thread_id=ctx.thread_id,
                    n_calls=len(pending_calls),
                ),
            )
        for call_index, call in enumerate(pending_calls):
            if reject_batch:
                err = _rejected_tool_batch_error(call, truncated=False)
                yield ToolCallStarted(
                    request_id=ctx.request_id,
                    tool=call.name,
                    label=call.name,
                    args=dict(call.args),
                    parse_error=call.parse_error,
                )
                event, response = _tool_outcome(
                    call, ctx.request_id, ToolExecutionResult.err(err)
                )
                yield event
                tool_results.append(response)
                continue

            # Provider couldn't parse streamed tool JSON: surface as a tool error
            # so the model can self-correct instead of executing with empty args.
            if call.parse_error:
                yield ToolCallStarted(
                    request_id=ctx.request_id,
                    tool=call.name,
                    label=call.name,
                    args=dict(call.args),
                    parse_error=call.parse_error,
                    call_id=call.call_id,
                )
                event, response = _tool_outcome(
                    call, ctx.request_id, ToolExecutionResult.err(call.parse_error)
                )
                yield event
                tool_results.append(response)
                continue

            inspector_gate = _RealtimeInspectorGate()
            async for evt in _resolve_realtime_inspector_decision(
                call=call,
                call_index=call_index,
                pending_calls=pending_calls,
                ctx=ctx,
                inspectors=inspectors,
                pending_bus=pending_bus,
                outcome=inspector_gate,
            ):
                yield evt
            if inspector_gate.aborted:
                tool_results.extend(inspector_gate.settled_responses)
                break

            if not inspector_gate.allowed:
                result = ToolExecutionResult.err(
                    inspector_gate.denial_message or "tool call denied"
                )
                inject_text = None
            else:
                yield ToolCallStarted(
                    request_id=ctx.request_id,
                    tool=call.name,
                    label=call.name,
                    args=dict(call.args),
                    call_id=call.call_id,
                )
                result, inject_text = await _execute_tool(
                    call,
                    ctx,
                    tool_executor=tool_executor,
                    hook_manager=hook_manager,
                    request_id=ctx.request_id,
                )
                if result.error is None and result.blocks:
                    resolved_blocks: list[ContentBlock] | None = None
                    async for item in _resolve_elicitation_blocks(
                        result.blocks,
                        ctx=ctx,
                        pending_bus=pending_bus,
                    ):
                        if isinstance(item, ActionRequiredEvent):
                            yield item
                        elif isinstance(item, list):
                            resolved_blocks = item
                    if resolved_blocks is not None:
                        result = ToolExecutionResult.ok_blocks(resolved_blocks)
                mcp_registry_mutated, loops_registry_mutated = _note_registry_mutation(
                    call,
                    result,
                    mcp_mutated=mcp_registry_mutated,
                    loops_mutated=loops_registry_mutated,
                )

            if inject_text and inject_texts_out is not None:
                inject_texts_out.append(inject_text)

            event, response = _tool_outcome(call, ctx.request_id, result)
            yield event
            tool_results.append(response)

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
            logger.info(
                "refreshed ctx.tools after MCP registry change (realtime) %s",
                kv(
                    request_id=ctx.request_id,
                    thread_id=ctx.thread_id,
                    tool_count=len(ctx.tools),
                ),
            )
            # Live vendor sessions fix tools at connect time; schemas are not
            # updated mid-session. Tell the model to start a new session if needed.
            if inject_texts_out is not None:
                inject_texts_out.append(_REALTIME_MCP_NEW_SESSION_NOTE)

        if loops_registry_mutated:
            advertised = bool(getattr(tool_executor, "loops_advertised", False))
            ctx = refresh_tools_after_loops_change(ctx, loops_advertised=advertised)
            logger.info(
                "refreshed ctx.tools after loops registry change (realtime) %s",
                kv(
                    request_id=ctx.request_id,
                    thread_id=ctx.thread_id,
                    loops_advertised=advertised,
                    tool_count=len(ctx.tools),
                ),
            )
            if inject_texts_out is not None:
                inject_texts_out.append(_REALTIME_LOOPS_NEW_SESSION_NOTE)

        # 5. Append all tool responses as one user message (reuses loop.py semantics).
        if tool_results:
            await history.append(
                ctx.thread_id,
                Message(role="user", content=tool_results),
            )

        # 6. Freeze attachments and emit descriptors.
        descriptor_events = await freeze_attachments_in_history(
            thread_id=ctx.thread_id,
            history=history,
            catalog=attachment_catalog,
            last_assistant_text=assistant_text.strip(),
        )
        for desc in descriptor_events:
            yield AttachmentDescriptorEvent(
                request_id=ctx.request_id,
                attachment_id=desc.attachment_id,
                mime_type=desc.mime_type,
                filename=desc.filename,
                description=desc.description,
            )

        await _fire_hook(
            hook_manager,
            event=HookEvent.POST_TURN,
            ctx=ctx,
            timeout_s=0,
        )

        logger.debug(
            "realtime turn end %s",
            kv(
                request_id=ctx.request_id,
                thread_id=ctx.thread_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                tool_results=len(tool_results),
            ),
        )

        # Make tool results available to the caller for later injection into the live session.
        if tool_results_out is not None:
            tool_results_out.extend(tool_results)
    except asyncio.CancelledError:
        try:
            cur = asyncio.current_task()
            if cur is not None and getattr(cur, "uncancel", None):
                cur.uncancel()
        except Exception:
            logger.warning(
                "realtime uncancel cleanup failed %s",
                kv(request_id=ctx.request_id, thread_id=ctx.thread_id),
                exc_info=True,
            )
        yield Error(request_id=ctx.request_id, error="Realtime turn cancelled")
        raise
    except Exception as exc:
        logger.exception(
            "realtime turn failed %s",
            kv(request_id=ctx.request_id, thread_id=ctx.thread_id),
        )
        yield Error(request_id=ctx.request_id, error=str(exc))
        raise
    finally:
        yield TurnComplete(request_id=ctx.request_id, usage=usage)
