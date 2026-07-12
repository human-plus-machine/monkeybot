"""Post-utterance processing for the realtime harness.

This module mirrors :func:`~monkeybot.core.runtime.loop.run` for realtime turns: it
commits finalized user/assistant utterances to ``HistoryStore``, runs hooks, and
dispatches tools. It does **not** call a model provider; the realtime vendor session
is managed by the gateway.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.attachments.catalog import SessionAttachmentCatalog
from monkeybot.core.attachments.freeze import freeze_attachments_in_history
from monkeybot.core.attachments.store import AttachmentStore
from monkeybot.core.context import (
    PendingResponseBusPort,
    TurnContext,
    MCP_REGISTRY_MUTATING_TOOLS,
    refresh_memory_index,
    refresh_tools_after_mcp_change,
)
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import Message, ToolCall
from monkeybot.core.llm.realtime_provider import RealtimeToolCall
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.persistence.transcript import TranscriptWriter
from monkeybot.core.runtime.events import (
    AgentEvent,
    AssistantDelta,
    AttachmentDescriptorEvent,
    Error,
    Thinking,
    ToolCallResult,
    ToolCallStarted,
    ToolConfirmationRequestEvent,
    TurnComplete,
    UsageTotals,
)
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.content_blocks import (
    ContentBlock,
    Text,
    ToolRequest,
    ToolResponse,
)

from .loop import ToolExecutorPort, _await_user_response_any

logger = logging.getLogger("monkeybot.core.runtime.realtime_loop")


_HOOK_PRE_TOOL_TIMEOUT_S = 1.5

_REALTIME_MCP_RECONNECT_NOTE = (
    "MCP registry updated on the harness. Newly enabled MCP tool schemas are not "
    "pushed into the live voice session; reconnect the realtime session to advertise "
    "them to the model."
)


def _user_text_from_content(blocks: Sequence[ContentBlock]) -> str:
    return " ".join(
        b.text.strip() for b in blocks if isinstance(b, Text) and b.text.strip()
    )


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
            await history.append(ctx.thread_id, Message(role="user", content=list(blocks)))
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
            await history.append(
                ctx.thread_id,
                Message(role="assistant", content=assistant_blocks),
            )

        # 4. Dispatch tools sequentially (v1: no parallel subagent dispatch here).
        mcp_registry_mutated = False
        for rtc in assistant_tool_calls:
            call = _realtime_tool_call_to_tool_call(rtc)

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

            # Inspectors (tool confirmation / deny) are applied if provided.
            allowed = True
            denial_message: str | None = None
            for insp in inspectors or []:
                from monkeybot.core.tools.inspector import InspectorToolCall

                decision = await insp.check(
                    InspectorToolCall(call_id=call.call_id, name=call.name, args=dict(call.args)),
                    ctx,
                )
                if decision.kind == "deny":
                    allowed = False
                    denial_message = decision.message
                    break
                if decision.kind == "confirm":
                    if pending_bus is None:
                        allowed = False
                        denial_message = (
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
                        break
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
                        allowed = False
                        denial_message = "cancelled by user"
                        break
                    if payload.get("_timeout"):
                        allowed = False
                        denial_message = "user did not respond in time"
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
                        break
                    if payload.get("approved"):
                        allowed = True
                    else:
                        allowed = False
                        reason_raw = payload.get("reason")
                        denial_message = (
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
                                reason=denial_message,
                            ),
                        )
                    break

            if not allowed:
                result = ToolExecutionResult.err(denial_message or "tool call denied")
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
                if (
                    result.error is None
                    and call.name in MCP_REGISTRY_MUTATING_TOOLS
                ):
                    mcp_registry_mutated = True

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
            # updated mid-session. Tell the model to reconnect if it needs them.
            if inject_texts_out is not None:
                inject_texts_out.append(_REALTIME_MCP_RECONNECT_NOTE)

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
