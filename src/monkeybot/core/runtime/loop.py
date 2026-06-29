"""Owned agent loop: provider streaming, inspectors, tool dispatch, event emission."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import shutil
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from monkeybot.core.attachments.catalog import SessionAttachmentCatalog
from monkeybot.core.attachments.freeze import freeze_attachments_in_history
from monkeybot.core.attachments.resolve import resolve_messages_for_provider
from monkeybot.core.attachments.store import AttachmentStore
from monkeybot.core.context import SkillRef, TurnContext, refresh_memory_index
from monkeybot.core.context.curator import (
    curation_enabled_from_env,
    curation_threshold_met,
    curator_model_id,
    run_context_curator,
)
from monkeybot.core.context.tool_output_policy import resolve_tool_budget
from monkeybot.core.context.tool_shapers import (
    exceeds_tool_output_budget,
    shape_messages_tool_results,
    shape_tool_text,
)
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import (
    Done,
    Message,
    Provider,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    UsageEvent,
)
from monkeybot.core.llm.retry import retrying_provider_stream
from monkeybot.core.llm.usage import Usage
from monkeybot.core.logging_utils import kv
from monkeybot.core.messages.tool_integrity import repair_tool_turn_integrity
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.prompts.prompt import compose_system_prompt, latest_user_message_text
from monkeybot.core.runtime.context_budget import (
    ContextBudgeter,
    compute_context_pressure_tier,
    summarization_trigger_ratio_from_env,
)
from monkeybot.core.tools.inspector import InspectorToolCall, ToolInspector
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
    AssistantDelta,
    AttachmentDescriptorEvent,
    ContextSummarized,
    ContextSummarizing,
    Error,
    ImageBlock,
    SystemPromptSnapshot,
    Thinking,
    ToolCallResult,
    ToolCallStarted,
    ToolConfirmationRequestEvent,
    TurnComplete,
    UsageTotals,
)

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


def _system_message(
    ctx: TurnContext,
    chat_messages: Sequence[Message],
    *,
    curated_memory_skills: bool = False,
    curated_memory_index: list[str] | None = None,
    curated_skills: list[SkillRef] | None = None,
    attachment_catalog: SessionAttachmentCatalog | None = None,
) -> Message:
    """System message: AGENT.md base plus runtime memory, skills, harness, and task anchor."""
    body = compose_system_prompt(
        ctx,
        chat_messages=chat_messages,
        curated_memory_skills=curated_memory_skills,
        curated_memory_index=curated_memory_index,
        curated_skills=curated_skills,
        attachment_catalog=(
            attachment_catalog.list_records() if attachment_catalog is not None else None
        ),
    )
    return Message(role="system", content=[Text(text=body)])


def _messages_for_provider(system: Message, history: Sequence[Message]) -> list[Message]:
    return [system, *list(history)]


async def _load_repaired_chat_history(history: HistoryStore, thread_id: str) -> list[Message]:
    """Load transcript rows and repair broken tool turns in memory before provider replay."""
    return repair_tool_turn_integrity(await history.load(thread_id))


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
SUMMARY_TRIGGER_RATIO = summarization_trigger_ratio_from_env()
"""Same ratio as pre-stream summarization check (``preflight_prompt_tokens >= cap``)."""
_SUMMARY_TRIGGER_RATIO = SUMMARY_TRIGGER_RATIO
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
    if provider.name not in ("gemini", "claude"):
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
) -> int:
    return await provider.count_input_tokens(messages, tools, model=model)


async def _prompt_input_tokens_for_history(
    *,
    ctx: TurnContext,
    chat_messages: Sequence[Message],
    provider: Provider,
    attachment_store: AttachmentStore | None,
    attachment_catalog: SessionAttachmentCatalog | None,
    curated_memory_skills: bool = False,
    curated_memory_index: list[str] | None = None,
    curated_skills: list[SkillRef] | None = None,
    extra_system_text: str | None = None,
) -> int:
    """Provider-accurate prompt size for history rows already persisted (e.g. post-assistant)."""
    system = _system_message(
        ctx,
        chat_messages,
        curated_memory_skills=curated_memory_skills,
        curated_memory_index=curated_memory_index,
        curated_skills=curated_skills,
        attachment_catalog=attachment_catalog,
    )
    system = _append_extra_system_text(system, extra_system_text)
    resolved_messages = resolve_messages_for_provider(
        chat_messages,
        attachment_store=attachment_store,
        session_id=ctx.thread_id,
    )
    provider_messages = _messages_for_provider(system, resolved_messages)
    return await _provider_prompt_input_tokens(
        provider, provider_messages, ctx.tools, model=ctx.model
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
        chat_messages = await _load_repaired_chat_history(history, ctx.thread_id)
        system = _system_message(ctx, chat_messages)
        provider_messages = _messages_for_provider(system, chat_messages)
        post = await _provider_prompt_input_tokens(
            provider, provider_messages, ctx.tools, model=ctx.model
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
    async for ev in retrying_provider_stream(
        provider,
        summarize_messages,
        [],
        model=model,
    ):
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
    run_id: str | None = None,
    cancelled: asyncio.Event | None = None,
    max_turns: int | None = None,
    hook_manager: HookManager | None = None,
    curator_provider: Provider | None = None,
    attachment_store: AttachmentStore | None = None,
    attachment_catalog: SessionAttachmentCatalog | None = None,
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
        logger.exception(
            "harness turn failed %s",
            kv(request_id=ctx.request_id, thread_id=ctx.thread_id),
        )
        yield Error(request_id=ctx.request_id, error=str(exc))
    finally:
        logger.debug(
            "harness run end %s",
            kv(
                request_id=ctx.request_id,
                thread_id=ctx.thread_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
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
    # Final assistant write is backgrounded off the streaming path; awaited at the
    # turn tail before any history load/reset so the row is never lost.
    assistant_write_task: asyncio.Task[None] | None = None
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

            chat_messages = await _load_repaired_chat_history(history, ctx.thread_id)
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

            if (
                turn_index == 1
                and ctx.context_curation_enabled
                and curation_enabled_from_env()
                and curation_threshold_met(ctx)
            ):
                curated_injection = True
                logger.debug(
                    "context curation start %s",
                    kv(request_id=ctx.request_id, thread_id=ctx.thread_id, turn=turn_index),
                )
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
                logger.debug(
                    "context curation done %s",
                    kv(
                        request_id=ctx.request_id,
                        thread_id=ctx.thread_id,
                        turn=turn_index,
                        memory_lines=len(curated_mem),
                        skills=len(curated_sks),
                        success=parts.success,
                    ),
                )

            system = _system_message(
                ctx,
                chat_messages,
                curated_memory_skills=curated_injection,
                curated_memory_index=curated_mem,
                curated_skills=curated_sks,
                attachment_catalog=attachment_catalog,
            )
            combined_extra = _combine_extras(pre_turn_extra, pre_tool_extra_next)
            system = _append_extra_system_text(system, combined_extra)
            pre_tool_extra_next = None
            resolved_messages = resolve_messages_for_provider(
                chat_messages,
                attachment_store=attachment_store,
                session_id=ctx.thread_id,
            )
            provider_messages = _messages_for_provider(system, resolved_messages)

            preflight = await _provider_prompt_input_tokens(
                provider, provider_messages, ctx.tools, model=ctx.model
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
                chat_messages = await _load_repaired_chat_history(history, ctx.thread_id)
                ctx = await refresh_memory_index(ctx)
                system = _system_message(
                    ctx,
                    chat_messages,
                    curated_memory_skills=curated_injection,
                    curated_memory_index=curated_mem,
                    curated_skills=curated_sks,
                    attachment_catalog=attachment_catalog,
                )
                system = _append_extra_system_text(system, pre_turn_extra)
                resolved_messages = resolve_messages_for_provider(
                    chat_messages,
                    attachment_store=attachment_store,
                    session_id=ctx.thread_id,
                )
                provider_messages = _messages_for_provider(system, resolved_messages)
                post = await _provider_prompt_input_tokens(
                    provider, provider_messages, ctx.tools, model=ctx.model
                )
                usage.estimated_prompt_tokens = max(usage.estimated_prompt_tokens, post)

            pressure_tier = compute_context_pressure_tier(
                usage.estimated_prompt_tokens,
                ctx.context_window_tokens,
            )
            if pressure_tier in ("moderate", "aggressive"):
                shaped_history = shape_messages_tool_results(
                    chat_messages,
                    protect_recent=_SUMMARY_KEEP_TAIL,
                    pressure_tier=pressure_tier,
                )
                resolved_messages = resolve_messages_for_provider(
                    shaped_history,
                    attachment_store=attachment_store,
                    session_id=ctx.thread_id,
                )
                provider_messages = _messages_for_provider(system, resolved_messages)

            yield SystemPromptSnapshot(
                request_id=ctx.request_id,
                inner_turn=turn_index,
                text=_system_prompt_snapshot_text(system),
            )

            pending: dict[str, ToolCall] = {}
            assistant_text = ""
            thinking_text = ""
            thinking_signature: str | None = None

            llm_input = 0
            llm_output = 0
            llm_cached = 0
            llm_cache_read = 0
            llm_cache_creation = 0
            try:
                stream_thinking = _stream_thinking_budget(provider, resolved_messages)
                async with span_llm(ctx=ctx):
                    async with aclosing(
                        cast(
                            Any,
                            retrying_provider_stream(
                                provider,
                                provider_messages,
                                ctx.tools,
                                model=ctx.model,
                                thinking_budget=stream_thinking,
                            ),
                        )
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
                            elif isinstance(ev, ToolCall):
                                pending[ev.call_id] = ev
                            elif isinstance(ev, Done):
                                break
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
            except asyncio.CancelledError:
                yield Error(request_id=ctx.request_id, error="Request cancelled")
                needs_followup_after_tools = False
                return
            except Exception as exc:
                logger.exception(
                    "provider stream failed %s",
                    kv(request_id=ctx.request_id, thread_id=ctx.thread_id, model=ctx.model),
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
                rows = await _load_repaired_chat_history(history, ctx.thread_id)
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

            for chunk in _chunk_tool_calls(ordered):
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
                        )
                        result_evt, tool_resp = _tool_outcome(
                            call, ctx.request_id, ToolExecutionResult.err(msg)
                        )
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
                    all_tool_responses.extend(chunk_responses)
                    continue

                parallel_tasks = all(c.name == "task" for c in allowed_exec)
                dispatch_mode = "parallel" if parallel_tasks else "serial"
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

                if not parallel_tasks:
                    call = allowed_exec[0]
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
                            pre_tool_extra_next = _combine_extras(
                                pre_tool_extra_next, pre_tool_payload.inject_text
                            )
                        _record_tool_hook_span_event("pre_tool", call.name)
                        try:
                            tool_result = await tool_executor.execute(
                                call=call, ctx=ctx
                            )
                        except asyncio.CancelledError:
                            yield Error(request_id=ctx.request_id, error="Request cancelled")
                            needs_followup_after_tools = False
                            return
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
                        record_tool_outcome(
                            _blocks_to_sse_summary(tool_result.blocks)
                            if tool_result.error is None
                            else None,
                            tool_result.error,
                        )
                        await _fire_hook(
                            hook_manager,
                            event=HookEvent.POST_TOOL,
                            ctx=ctx,
                            timeout_s=0,
                            tool_name=call.name,
                            tool_args=dict(call.args),
                            tool_result=(
                                _blocks_to_sse_summary(tool_result.blocks)
                                if tool_result.error is None
                                else None
                            ),
                            tool_error=tool_result.error,
                        )
                        _record_tool_hook_span_event("post_tool", call.name)

                    event, response = _tool_outcome(call, ctx.request_id, tool_result)
                    yield event
                    for img_evt in _image_events(ctx.request_id, call.call_id, tool_result):
                        yield img_evt
                    chunk_responses.append(response)
                else:
                    if cancelled is not None and cancelled.is_set():
                        yield Error(request_id=ctx.request_id, error="Request cancelled")
                        needs_followup_after_tools = False
                        return

                    sem = asyncio.Semaphore(_MAX_CONCURRENT_SUBAGENTS)

                    async def _run_task(
                        call: ToolCall,
                        *,
                        _sem: asyncio.Semaphore = sem,
                        _ctx: TurnContext = ctx,
                    ) -> tuple[ToolCall, ToolExecutionResult]:
                        async with _sem, span_tool(
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
                                nonlocal pre_tool_extra_next
                                pre_tool_extra_next = _combine_extras(
                                    pre_tool_extra_next, pre_tool_payload.inject_text
                                )
                            _record_tool_hook_span_event("pre_tool", call.name)
                            logger.debug(
                                "tool execute %s",
                                kv(
                                    request_id=_ctx.request_id,
                                    thread_id=_ctx.thread_id,
                                    tool=call.name,
                                    call_id=call.call_id,
                                ),
                            )
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
                            record_tool_outcome(
                                _blocks_to_sse_summary(tool_result.blocks)
                                if tool_result.error is None
                                else None,
                                tool_result.error,
                            )
                            await _fire_hook(
                                hook_manager,
                                event=HookEvent.POST_TOOL,
                                ctx=_ctx,
                                timeout_s=0,
                                tool_name=call.name,
                                tool_args=dict(call.args),
                                tool_result=(
                                    _blocks_to_sse_summary(tool_result.blocks)
                                    if tool_result.error is None
                                    else None
                                ),
                                tool_error=tool_result.error,
                            )
                            _record_tool_hook_span_event("post_tool", call.name)
                            return (call, tool_result)

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

                        event, response = _tool_outcome(call, ctx.request_id, tool_result)
                        yield event
                        for img_evt in _image_events(ctx.request_id, call.call_id, tool_result):
                            yield img_evt
                        chunk_responses.append(response)

                all_tool_responses.extend(chunk_responses)

            if all_tool_responses:
                chat_for_budget = await _load_repaired_chat_history(history, ctx.thread_id)
                budget_used = usage.estimated_prompt_tokens
                try:
                    budget_used = await _prompt_input_tokens_for_history(
                        ctx=ctx,
                        chat_messages=chat_for_budget,
                        provider=provider,
                        attachment_store=attachment_store,
                        attachment_catalog=attachment_catalog,
                        curated_memory_skills=curated_injection,
                        curated_memory_index=curated_mem,
                        curated_skills=curated_sks,
                        extra_system_text=pre_turn_extra,
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
                ):
                    yield budget_evt

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
