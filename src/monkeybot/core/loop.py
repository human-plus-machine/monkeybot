"""Owned agent loop: provider streaming, inspectors, tool dispatch, event emission."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import Protocol, runtime_checkable

from monkeybot.core.context import TurnContext
from monkeybot.core.harness_prompt import harness_fixed_context
from monkeybot.core.events import (
    AgentEvent,
    AssistantDelta,
    ContextSummarized,
    ContextSummarizing,
    Error,
    Thinking,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
    UsageTotals,
)
from monkeybot.core.inspector import InspectorToolCall, ToolInspector
from monkeybot.core.provider import Done, Message, Provider, TextDelta, ToolCall, UsageEvent
from monkeybot.core.usage import Usage


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


def _system_message(ctx: TurnContext) -> Message:
    """Single system message: AGENT.md, memory index, skills, then harness tool wiring."""
    memory_bullets = "\n".join(f"- {line}" for line in ctx.memory_index) if ctx.memory_index else ""
    mem_block = f"\n\n## Memory index\n{memory_bullets}" if memory_bullets else ""

    skill_lines = []
    for s in ctx.skills:
        skill_lines.append(f"- {s.name}: {s.description} (entry: {s.entry_point})")
    skills_block = "\n".join(skill_lines)
    skills_section = f"\n\n## Skills\n{skills_block}" if skills_block else ""

    include_task = any(t.name == "task" for t in ctx.tools)
    harness = harness_fixed_context(include_task_tool=include_task)
    body = f"{ctx.agent_md}{mem_block}{skills_section}\n\n{harness}"
    return Message(role="system", content=body)


def _messages_for_provider(system: Message, history: Sequence[Message]) -> list[Message]:
    return [system, *list(history)]


def _assistant_tool_placeholder(text: str, calls: Sequence[ToolCall]) -> str:
    """Stable, parseable assistant row when the model requested tools (batched until Done)."""
    payload = {
        "tool_calls": [
            {"call_id": c.call_id, "name": c.name, "args": c.args} for c in calls
        ]
    }
    tail = json.dumps(payload, ensure_ascii=False)
    if text:
        return f"{text}\n{tail}"
    return tail


# Max concurrent ``task`` (subagent) subprocesses per single model tool batch.
_MAX_CONCURRENT_SUBAGENTS = 10

_SPILL_REL = Path(".monkeybot") / "spill"
_SUMMARY_TRIGGER_RATIO = 0.85
_SUMMARY_KEEP_HEAD = 1
_SUMMARY_KEEP_TAIL = 6


def _estimate_tokens(messages: Sequence[Message]) -> int:
    return sum(len(m.content) for m in messages) // 4


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
    lines: list[str] = []
    for m in middle:
        label = m.role
        if m.tool_name:
            label = f"{m.role} tool={m.tool_name}"
        lines.append(f"[{label}]\n{m.content}")
    blob = "\n\n---\n\n".join(lines)
    summarize_messages = [
        Message(
            role="system",
            content=(
                "You compress prior agent conversation turns into one dense factual summary. "
                "Preserve decisions, file paths, errors, tool outcomes, and open tasks. "
                "Output prose only, no markdown headings unless essential."
            ),
        ),
        Message(
            role="user",
            content="Summarize the following conversation segment:\n\n" + blob,
        ),
    ]
    summary_text = ""
    async with aclosing(provider.stream(summarize_messages, [], model=model)) as stream:
        async for ev in stream:
            if isinstance(ev, TextDelta):
                summary_text += ev.text
            elif isinstance(ev, Done):
                break
    summary_text = summary_text.strip() or "(empty summary)"
    merged = [
        *head,
        Message(role="assistant", content=f"[Context Summary]: {summary_text}"),
        *tail,
    ]
    await history.reset(thread_id, merged)
    return len(middle)


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
) -> AsyncIterator[AgentEvent]:
    """Stream agent events for one user message; ends with ``TurnComplete`` (never raises).

    Provider chunks are handled in Gemini-style batches: tool calls accumulate until ``Done``,
    then execute in lexicographic ``call_id`` order for deterministic replay.

    Consecutive ``task`` tool calls in one batch run concurrently (at most 10 at a time);
    other tools run one chunk at a time in order.
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
) -> AsyncIterator[AgentEvent]:
    effective_max = _effective_max_turns(max_turns)
    _ = await history.load(ctx.thread_id)
    if ctx.workspace_root is not None:
        _cleanup_spill_files(ctx.workspace_root, ctx.thread_id)
    await history.append(ctx.thread_id, Message(role="user", content=message))

    turn_index = 0
    needs_followup_after_tools = False

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
        system = _system_message(ctx)
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
            provider_messages = _messages_for_provider(system, chat_messages)

        pending: dict[str, ToolCall] = {}
        assistant_text = ""

        try:
            async with aclosing(provider.stream(provider_messages, ctx.tools, model=ctx.model)) as stream:
                async for ev in stream:
                    if isinstance(ev, TextDelta):
                        assistant_text += ev.text
                        if ev.text:
                            yield AssistantDelta(request_id=ctx.request_id, delta=ev.text)
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
            if assistant_text.strip():
                await history.append(
                    ctx.thread_id,
                    Message(role="assistant", content=assistant_text),
                )
                needs_followup_after_tools = False
                break
            # Model returned no text after tool results (or only whitespace). Without another
            # provider round the user sees tools then silence — retry until turn budget.
            rows = await history.load(ctx.thread_id)
            owes_tool_followup = needs_followup_after_tools or (
                bool(rows) and rows[-1].role == "tool"
            )
            if owes_tool_followup and turn_index < effective_max:
                continue
            needs_followup_after_tools = False
            break

        ordered = sorted(pending.values(), key=lambda c: c.call_id)
        await history.append(
            ctx.thread_id,
            Message(role="assistant", content=_assistant_tool_placeholder(assistant_text, ordered)),
        )

        needs_followup_after_tools = True

        for chunk in _chunk_tool_calls(ordered):
            if cancelled is not None and cancelled.is_set():
                yield Error(request_id=ctx.request_id, error="Request cancelled")
                needs_followup_after_tools = False
                return

            allowed_exec: list[ToolCall] = []

            for call in chunk:
                if cancelled is not None and cancelled.is_set():
                    yield Error(request_id=ctx.request_id, error="Request cancelled")
                    needs_followup_after_tools = False
                    return

                inspector_call = InspectorToolCall(
                    call_id=call.call_id, name=call.name, args=call.args
                )
                allowed = True
                denial_message: str | None = None
                for insp in inspectors:
                    decision = await insp.check(inspector_call, ctx)
                    if decision.kind == "deny":
                        allowed = False
                        denial_message = decision.message
                        break

                label = call.name
                args_obj: dict[str, object] = dict(call.args)

                if not allowed:
                    msg = denial_message or "tool call denied"
                    yield ToolCallStarted(
                        request_id=ctx.request_id,
                        tool=call.name,
                        label=label,
                        args=args_obj,
                    )
                    yield Error(request_id=ctx.request_id, error=msg)
                    await history.append(
                        ctx.thread_id,
                        Message(
                            role="tool",
                            content=msg,
                            tool_name=call.name,
                            tool_call_id=call.call_id,
                        ),
                    )
                    continue

                yield ToolCallStarted(
                    request_id=ctx.request_id,
                    tool=call.name,
                    label=label,
                    args=args_obj,
                )
                allowed_exec.append(call)

            if not allowed_exec:
                continue

            parallel_tasks = all(c.name == "task" for c in allowed_exec)

            if not parallel_tasks:
                call = allowed_exec[0]
                try:
                    result_text, err_text = await tool_executor.execute(call=call, ctx=ctx)
                except asyncio.CancelledError:
                    yield Error(request_id=ctx.request_id, error="Request cancelled")
                    needs_followup_after_tools = False
                    return
                except Exception as exc:
                    err_text = str(exc)
                    result_text = None

                if err_text is not None:
                    yield ToolCallResult(
                        request_id=ctx.request_id,
                        tool=call.name,
                        result="",
                        error=err_text,
                    )
                    await history.append(
                        ctx.thread_id,
                        Message(
                            role="tool",
                            content=err_text,
                            tool_name=call.name,
                            tool_call_id=call.call_id,
                        ),
                    )
                else:
                    body = result_text or ""
                    yield ToolCallResult(
                        request_id=ctx.request_id,
                        tool=call.name,
                        result=body,
                        error=None,
                    )
                    await history.append(
                        ctx.thread_id,
                        Message(
                            role="tool",
                            content=body,
                            tool_name=call.name,
                            tool_call_id=call.call_id,
                        ),
                    )
            else:
                if cancelled is not None and cancelled.is_set():
                    yield Error(request_id=ctx.request_id, error="Request cancelled")
                    needs_followup_after_tools = False
                    return

                sem = asyncio.Semaphore(_MAX_CONCURRENT_SUBAGENTS)

                async def _run_task(call: ToolCall) -> tuple[ToolCall, str | None, str | None]:
                    async with sem:
                        try:
                            rt, et = await tool_executor.execute(call=call, ctx=ctx)
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
                        _, result_text, err_text = outcome

                    if err_text is not None:
                        yield ToolCallResult(
                            request_id=ctx.request_id,
                            tool=call.name,
                            result="",
                            error=err_text,
                        )
                        await history.append(
                            ctx.thread_id,
                            Message(
                                role="tool",
                                content=err_text,
                                tool_name=call.name,
                                tool_call_id=call.call_id,
                            ),
                        )
                    else:
                        body = result_text or ""
                        yield ToolCallResult(
                            request_id=ctx.request_id,
                            tool=call.name,
                            result=body,
                            error=None,
                        )
                        await history.append(
                            ctx.thread_id,
                            Message(
                                role="tool",
                                content=body,
                                tool_name=call.name,
                                tool_call_id=call.call_id,
                            ),
                        )

    if needs_followup_after_tools:
        yield Error(request_id=ctx.request_id, error="Max turns exceeded")
