"""Owned agent loop: provider streaming, inspectors, tool dispatch, event emission."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from monkeybot.core.types.content_blocks import (
    ContentBlock,
    Text,
    ToolRequest,
    ToolResponse,
)
from monkeybot.core.types.content_blocks import Thinking as ThinkingBlock
from monkeybot.core.context import SkillRef, TurnContext, refresh_memory_index
from monkeybot.core.context.curator import (
    curation_enabled_from_env,
    curation_threshold_met,
    curator_model_id,
    run_context_curator,
)
from .events import (
    AgentEvent,
    AssistantDelta,
    ContextSummarized,
    ContextSummarizing,
    Error,
    SystemPromptSnapshot,
    Thinking,
    ToolCallResult,
    ToolCallStarted,
    ToolConfirmationRequestEvent,
    TurnComplete,
    UsageTotals,
)
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.tools.inspector import InspectorToolCall, ToolInspector
from monkeybot.core.prompts.prompt import compose_system_prompt, latest_user_message_text
from monkeybot.core.llm.provider import (
    Done,
    Message,
    Provider,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    UsageEvent,
)
from monkeybot.core.llm.usage import Usage


def _effective_max_turns(max_turns: int | None) -> int:
    if max_turns is not None:
        return max_turns
    return int(os.getenv("MAX_TURNS", "50"))


def _usage_to_totals(u: Usage) -> UsageTotals:
    return UsageTotals(
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cached_tokens=u.cached_tokens,
        cost_usd=u.cost_usd,
        duration_ms=u.duration_ms,
    )


def _merge_usage_event(usage: Usage, ev: UsageEvent) -> None:
    usage.input_tokens += ev.input_tokens
    usage.output_tokens += ev.output_tokens
    usage.cached_tokens += ev.cached_tokens


def _system_message(
    ctx: TurnContext,
    chat_messages: Sequence[Message],
    *,
    curated_memory_skills: bool = False,
    curated_memory_index: list[str] | None = None,
    curated_skills: list[SkillRef] | None = None,
) -> Message:
    """System message: AGENT.md base plus runtime memory, skills, harness, and task anchor."""
    body = compose_system_prompt(
        ctx,
        chat_messages=chat_messages,
        curated_memory_skills=curated_memory_skills,
        curated_memory_index=curated_memory_index,
        curated_skills=curated_skills,
    )
    return Message(role="system", content=[Text(text=body)])


def _messages_for_provider(system: Message, history: Sequence[Message]) -> list[Message]:
    return [system, *list(history)]


_HOOK_READ_TIMEOUT_S = 2.0
_HOOK_PRE_TOOL_TIMEOUT_S = 1.5


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
    )
    return await hook_manager.fire(payload, timeout_s=timeout_s)


def _append_extra_system_text(system: Message, extra: str | None) -> Message:
    """Return a new system Message with ``extra`` under a ``## Runtime notes`` section.

    Uses the same markdown heading style as the rest of the composed system prompt
    (``## Memory index``, harness sections). When ``extra`` is empty/None the original
    message is returned unchanged.
    """
    if not extra:
        return system
    base = "".join(b.text for b in system.content if isinstance(b, Text))
    wrapped = f"{base}\n\n## Runtime notes\n\n{extra.strip()}\n"
    return Message(role="system", content=[Text(text=wrapped)])


def _combine_extras(*parts: str | None) -> str | None:
    """Join non-empty hook-injected fragments with blank lines; ``None`` if all empty."""
    kept = [p.strip() for p in parts if p and p.strip()]
    if not kept:
        return None
    return "\n\n".join(kept)


# Max concurrent ``task`` (subagent) subprocesses per single model tool batch.
_MAX_CONCURRENT_SUBAGENTS = 10

_SPILL_REL = Path(".monkeybot") / "spill"
_SUMMARY_TRIGGER_RATIO = 0.85
_SUMMARY_KEEP_HEAD = 1
_SUMMARY_KEEP_TAIL = 6


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


def _char_estimate_blocks(blocks: Sequence[ContentBlock]) -> int:
    """Rough character count for token heuristics (Story 4 block-native history)."""
    total = 0
    for b in blocks:
        if isinstance(b, Text):
            total += len(b.text)
        elif isinstance(b, ToolRequest):
            total += len(b.id) + len(b.name) + len(json.dumps(b.args, sort_keys=True))
        elif isinstance(b, ToolResponse):
            total += len(b.id) + len(b.tool_name) + _char_estimate_blocks(b.result)
        else:
            total += len(json.dumps(b.to_dict(), sort_keys=True))
    return total


def _flatten_tool_result_for_summary(resp: ToolResponse) -> str:
    parts: list[str] = []
    for b in resp.result:
        if isinstance(b, Text):
            parts.append(b.text)
        else:
            parts.append(json.dumps(b.to_dict(), sort_keys=True))
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


def _system_prompt_snapshot_text(system: Message) -> str:
    """Plain string for :class:`SystemPromptSnapshot` (composed prompt lives in Text blocks)."""
    return "".join(b.text for b in system.content if isinstance(b, Text))


def _estimate_tokens(messages: Sequence[Message]) -> int:
    return sum(_char_estimate_blocks(m.content) for m in messages) // 4


def _cleanup_spill_files(workspace_root: Path, thread_id: str) -> None:
    spill_path = Path(workspace_root).resolve() / _SPILL_REL / thread_id
    if spill_path.exists():
        shutil.rmtree(spill_path, ignore_errors=True)


def _summarization_viable(messages: Sequence[Message]) -> bool:
    return len(messages) > _SUMMARY_KEEP_HEAD + _SUMMARY_KEEP_TAIL


async def _summarize_history(
    thread_id: str,
    messages: list[Message],
    history: ConversationHistoryPort,
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
            content=[
                Text(
                    text=(
                        "You compress prior agent conversation turns into one dense factual summary. "
                        "Preserve decisions, file paths, errors, tool outcomes, and open tasks. "
                        "Output prose only, no markdown headings unless essential."
                    )
                )
            ],
        ),
        Message(
            role="user",
            content=[
                Text(text="Summarize the following conversation segment:\n\n" + blob)
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
            content=[Text(text=f"[Context Summary]: {summary_text}")],
        ),
        *tail,
    ]
    await history.reset(thread_id, merged)
    return len(middle)


def _tool_outcome(
    call: ToolCall,
    request_id: str,
    result_text: str | None,
    err_text: str | None,
) -> tuple[ToolCallResult, ToolResponse]:
    """Build the (event, history block) pair for a finished tool call.

    Used by both the sequential and parallel ``task`` dispatch paths so that
    result formatting stays in one place.
    """
    is_error = err_text is not None
    body = "" if is_error else (result_text or "")
    text = err_text if is_error else body
    event = ToolCallResult(
        request_id=request_id,
        tool=call.name,
        result=body,
        error=err_text,
    )
    response = ToolResponse(
        id=call.call_id,
        tool_name=call.name,
        result=[Text(text=text or "")],
        is_error=is_error,
    )
    return event, response


def _chunk_tool_calls(ordered: Sequence[ToolCall]) -> list[list[ToolCall]]:
    """Split into maximal runs of consecutive ``task`` tools vs single non-``task`` tools.

    Consecutive ``task`` calls may run concurrently (bounded by :data:`_MAX_CONCURRENT_SUBAGENTS`);
    other tools stay sequential chunk-by-chunk to preserve cancellation and side-effect ordering.
    """
    seq = list(ordered)
    chunks: list[list[ToolCall]] = []
    i = 0
    n = len(seq)
    while i < n:
        if seq[i].name == "task":
            j = i + 1
            while j < n and seq[j].name == "task":
                j += 1
            chunks.append(seq[i:j])
            i = j
        else:
            chunks.append([seq[i]])
            i += 1
    return chunks


@runtime_checkable
class ConversationHistoryPort(Protocol):
    async def load(self, thread_id: str, limit: int = 100) -> list[Message]: ...

    async def append(self, thread_id: str, message: Message) -> None: ...

    async def reset(self, thread_id: str, messages: list[Message]) -> None: ...


@runtime_checkable
class ToolExecutorPort(Protocol):
    """Fakeable tool execution boundary (Story 6 does not invoke real shell)."""

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> tuple[str | None, str | None]:
        """``(result_text, error_text)`` — success ``(text, None)``; failure ``(None, message)``."""


async def run(
    message: str,
    ctx: TurnContext,
    *,
    provider: Provider,
    history: ConversationHistoryPort,
    inspectors: list[ToolInspector],
    tool_executor: ToolExecutorPort,
    run_id: str | None = None,
    cancelled: asyncio.Event | None = None,
    max_turns: int | None = None,
    hook_manager: HookManager | None = None,
    curator_provider: Provider | None = None,
) -> AsyncIterator[AgentEvent]:
    """Stream agent events for one user message; ends with ``TurnComplete`` (never raises).

    Provider chunks are handled in Gemini-style batches: tool calls accumulate until ``Done``,
    then execute in lexicographic ``call_id`` order for deterministic replay.

    Consecutive ``task`` tool calls in one batch run concurrently (at most 10 at a time);
    other tools run one chunk at a time in order.

    ``curator_provider`` is an optional dedicated provider for context curation (e.g. with
    ``thinking_budget=0``). Falls back to ``provider`` when not supplied.
    """
    del run_id  # reserved for durable runs / gateway wiring
    usage = Usage()
    try:
        async for evt in _run_inner(
            message,
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
        ):
            yield evt
    except asyncio.CancelledError:
        try:
            cur = asyncio.current_task()
            if cur is not None and getattr(cur, "uncancel", None):
                cur.uncancel()
        except Exception:
            pass
        yield Error(request_id=ctx.request_id, error="Request cancelled")
    except Exception as exc:
        yield Error(request_id=ctx.request_id, error=str(exc))
    finally:
        yield TurnComplete(request_id=ctx.request_id, usage=_usage_to_totals(usage))


async def _run_inner(
    message: str,
    ctx: TurnContext,
    *,
    provider: Provider,
    history: ConversationHistoryPort,
    inspectors: list[ToolInspector],
    tool_executor: ToolExecutorPort,
    cancelled: asyncio.Event | None,
    max_turns: int | None,
    usage: Usage,
    hook_manager: HookManager | None = None,
    curator_provider: Provider | None = None,
) -> AsyncIterator[AgentEvent]:
    effective_max = _effective_max_turns(max_turns)
    _ = await history.load(ctx.thread_id)
    if ctx.workspace_root is not None:
        _cleanup_spill_files(ctx.workspace_root, ctx.thread_id)
    await history.append(ctx.thread_id, Message.text("user", message))

    await _fire_hook(
        hook_manager,
        event=HookEvent.USER_MESSAGE,
        ctx=ctx,
        timeout_s=0,
        user_message=message,
    )

    turn_index = 0
    needs_followup_after_tools = False
    curated_injection = False
    curated_mem: list[str] = []
    curated_sks: list[SkillRef] = []
    pre_turn_extra: str | None = None
    pre_tool_extra_next: str | None = None

    while turn_index < effective_max:
        if cancelled is not None and cancelled.is_set():
            yield Error(request_id=ctx.request_id, error="Request cancelled")
            needs_followup_after_tools = False
            break

        turn_index += 1
        yield Thinking(request_id=ctx.request_id)

        if cancelled is not None and cancelled.is_set():
            yield Error(request_id=ctx.request_id, error="Request cancelled")
            needs_followup_after_tools = False
            break

        chat_messages = await history.load(ctx.thread_id)
        ctx = await refresh_memory_index(ctx)

        if turn_index == 1:
            pre_turn_payload = await _fire_hook(
                hook_manager,
                event=HookEvent.PRE_TURN,
                ctx=ctx,
                timeout_s=_HOOK_READ_TIMEOUT_S,
                user_message=message,
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

        if (
            turn_index == 1
            and ctx.context_curation_enabled
            and curation_enabled_from_env()
            and curation_threshold_met(ctx)
        ):
            curated_injection = True
            u = latest_user_message_text(chat_messages) or ""
            parts = await run_context_curator(
                ctx=ctx,
                provider=provider,
                curator_model=curator_model_id(ctx),
                user_message=u,
                curator_provider=curator_provider,
            )
            if parts.success:
                curated_mem = list(parts.memory_lines)
                curated_sks = list(parts.skills)
            else:
                curated_mem = []
                curated_sks = []

        system = _system_message(
            ctx,
            chat_messages,
            curated_memory_skills=curated_injection,
            curated_memory_index=curated_mem,
            curated_skills=curated_sks,
        )
        combined_extra = _combine_extras(pre_turn_extra, pre_tool_extra_next)
        system = _append_extra_system_text(system, combined_extra)
        pre_tool_extra_next = None
        provider_messages = _messages_for_provider(system, chat_messages)

        estimated = _estimate_tokens(provider_messages)
        cap = max(1, int(ctx.context_window_tokens * _SUMMARY_TRIGGER_RATIO))
        if estimated >= cap and _summarization_viable(chat_messages):
            yield ContextSummarizing(
                request_id=ctx.request_id,
                estimated_tokens=estimated,
                context_window_tokens=ctx.context_window_tokens,
            )
            try:
                turns_summarized = await _summarize_history(
                    ctx.thread_id,
                    chat_messages,
                    history,
                    provider,
                    ctx.model,
                )
            except Exception:
                turns_summarized = 0
            yield ContextSummarized(
                request_id=ctx.request_id,
                turns_summarized=turns_summarized,
            )
            chat_messages = await history.load(ctx.thread_id)
            ctx = await refresh_memory_index(ctx)
            system = _system_message(
                ctx,
                chat_messages,
                curated_memory_skills=curated_injection,
                curated_memory_index=curated_mem,
                curated_skills=curated_sks,
            )
            system = _append_extra_system_text(system, pre_turn_extra)
            provider_messages = _messages_for_provider(system, chat_messages)

        yield SystemPromptSnapshot(
            request_id=ctx.request_id,
            inner_turn=turn_index,
            text=_system_prompt_snapshot_text(system),
        )

        pending: dict[str, ToolCall] = {}
        assistant_text = ""
        thinking_text = ""
        thinking_signature: str | None = None

        try:
            async with aclosing(
                cast(Any, provider.stream(provider_messages, ctx.tools, model=ctx.model))
            ) as stream:
                async for ev in stream:
                    if isinstance(ev, TextDelta):
                        assistant_text += ev.text
                        if ev.text:
                            yield AssistantDelta(request_id=ctx.request_id, delta=ev.text)
                    elif isinstance(ev, ThinkingDelta):
                        thinking_text += ev.text
                        if ev.signature:
                            thinking_signature = ev.signature
                    elif isinstance(ev, UsageEvent):
                        _merge_usage_event(usage, ev)
                    elif isinstance(ev, ToolCall):
                        pending[ev.call_id] = ev
                    elif isinstance(ev, Done):
                        break
        except asyncio.CancelledError:
            yield Error(request_id=ctx.request_id, error="Request cancelled")
            needs_followup_after_tools = False
            return
        except Exception as exc:
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
                await history.append(
                    ctx.thread_id,
                    Message(role="assistant", content=[Text(text=cleaned_text)]),
                )
                needs_followup_after_tools = False
                break
            # Model returned no text after tool results (or only whitespace). Without another
            # provider round the user sees tools then silence — retry until turn budget.
            rows = await history.load(ctx.thread_id)
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
                    metadata=dict(c.metadata) if c.metadata else None,
                )
            )
        await history.append(
            ctx.thread_id,
            Message(role="assistant", content=assist_blocks),
        )

        needs_followup_after_tools = True

        for chunk in _chunk_tool_calls(ordered):
            if cancelled is not None and cancelled.is_set():
                yield Error(request_id=ctx.request_id, error="Request cancelled")
                needs_followup_after_tools = False
                return

            allowed_exec: list[ToolCall] = []
            # Collect all ToolResponse blocks for this chunk so they can be
            # appended in a single user Message. Gemini requires that the number
            # of functionResponse parts equals the number of functionCall parts
            # in the preceding model turn — splitting them across separate
            # Message rows produces a 400 INVALID_ARGUMENT error.
            chunk_responses: list[ToolResponse] = []

            for call in chunk:
                if cancelled is not None and cancelled.is_set():
                    yield Error(request_id=ctx.request_id, error="Request cancelled")
                    needs_followup_after_tools = False
                    return

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
                                allowed = False
                                denial_message = "cancelled by user"
                                break
                            if payload.get("_timeout"):
                                allowed = False
                                to = int(
                                    float(os.environ.get("PENDING_RESPONSE_TIMEOUT_SEC", "300"))
                                )
                                denial_message = f"user did not respond within {to}s"
                                break
                            if payload.get("approved"):
                                allowed = True
                            else:
                                allowed = False
                                reason_raw = payload.get("reason")
                                denial_message = (
                                    (reason_raw if isinstance(reason_raw, str) else None)
                                    or "denied by user"
                                )
                            break

                if not allowed:
                    msg = denial_message or "tool call denied"
                    yield ToolCallStarted(
                        request_id=ctx.request_id,
                        tool=call.name,
                        label=call.name,
                        args=dict(call.args),
                    )
                    result_evt, tool_resp = _tool_outcome(call, ctx.request_id, None, msg)
                    yield result_evt
                    chunk_responses.append(tool_resp)
                    continue

                yield ToolCallStarted(
                    request_id=ctx.request_id,
                    tool=call.name,
                    label=call.name,
                    args=dict(call.args),
                )
                allowed_exec.append(call)

            if not allowed_exec:
                if chunk_responses:
                    await history.append(
                        ctx.thread_id,
                        Message(role="user", content=chunk_responses),
                    )
                continue

            parallel_tasks = all(c.name == "task" for c in allowed_exec)

            if not parallel_tasks:
                call = allowed_exec[0]
                pre_tool_payload = await _fire_hook(
                    hook_manager,
                    event=HookEvent.PRE_TOOL,
                    ctx=ctx,
                    timeout_s=_HOOK_PRE_TOOL_TIMEOUT_S,
                    tool_name=call.name,
                    tool_args=dict(call.args),
                )
                if pre_tool_payload is not None and pre_tool_payload.inject_text:
                    pre_tool_extra_next = _combine_extras(
                        pre_tool_extra_next, pre_tool_payload.inject_text
                    )
                try:
                    result_text, err_text = await tool_executor.execute(call=call, ctx=ctx)
                except asyncio.CancelledError:
                    yield Error(request_id=ctx.request_id, error="Request cancelled")
                    needs_followup_after_tools = False
                    return
                except Exception as exc:
                    err_text = str(exc)
                    result_text = None

                await _fire_hook(
                    hook_manager,
                    event=HookEvent.POST_TOOL,
                    ctx=ctx,
                    timeout_s=0,
                    tool_name=call.name,
                    tool_args=dict(call.args),
                    tool_result=result_text,
                    tool_error=err_text,
                )

                event, response = _tool_outcome(
                    call, ctx.request_id, result_text, err_text
                )
                yield event
                chunk_responses.append(response)
            else:
                if cancelled is not None and cancelled.is_set():
                    yield Error(request_id=ctx.request_id, error="Request cancelled")
                    needs_followup_after_tools = False
                    return

                for c in allowed_exec:
                    pre_tool_payload = await _fire_hook(
                        hook_manager,
                        event=HookEvent.PRE_TOOL,
                        ctx=ctx,
                        timeout_s=_HOOK_PRE_TOOL_TIMEOUT_S,
                        tool_name=c.name,
                        tool_args=dict(c.args),
                    )
                    if pre_tool_payload is not None and pre_tool_payload.inject_text:
                        pre_tool_extra_next = _combine_extras(
                            pre_tool_extra_next, pre_tool_payload.inject_text
                        )

                sem = asyncio.Semaphore(_MAX_CONCURRENT_SUBAGENTS)

                async def _run_task(
                    call: ToolCall,
                    *,
                    _sem: asyncio.Semaphore = sem,
                    _ctx: TurnContext = ctx,
                ) -> tuple[ToolCall, str | None, str | None]:
                    async with _sem:
                        try:
                            rt, et = await tool_executor.execute(call=call, ctx=_ctx)
                            return (call, rt, et)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            return (call, None, str(exc))

                try:
                    outcomes = await asyncio.gather(
                        *[_run_task(c) for c in allowed_exec],
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
                        err_text = str(outcome)
                        result_text = None
                    else:
                        _, result_text, err_text = cast(
                            tuple[ToolCall, str | None, str | None], outcome
                        )

                    await _fire_hook(
                        hook_manager,
                        event=HookEvent.POST_TOOL,
                        ctx=ctx,
                        timeout_s=0,
                        tool_name=call.name,
                        tool_args=dict(call.args),
                        tool_result=result_text,
                        tool_error=err_text,
                    )

                    event, response = _tool_outcome(
                        call, ctx.request_id, result_text, err_text
                    )
                    yield event
                    chunk_responses.append(response)

            if chunk_responses:
                await history.append(
                    ctx.thread_id,
                    Message(role="user", content=chunk_responses),
                )

    if needs_followup_after_tools:
        yield Error(request_id=ctx.request_id, error="Max turns exceeded")

    await _fire_hook(
        hook_manager,
        event=HookEvent.POST_TURN,
        ctx=ctx,
        timeout_s=0,
    )
