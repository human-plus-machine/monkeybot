"""History compaction / summarization and budgeted tool-response append."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from typing import Any, cast

from monkeybot.core.context import TurnContext
from monkeybot.core.context.epoch import ContextEpochTracker
from monkeybot.core.llm.provider import Done, Message, Provider, TextDelta
from monkeybot.core.llm.usage import Usage
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.runtime.context_budget import (
    ContextBudgeter,
    summarization_trigger_ratio_from_env,
)
from monkeybot.core.types.content_blocks import ContentBlock, Text

from .events import AgentEvent, ContextSummarized, ContextSummarizing
from .loop_messages import _load_agent_chat_history, _summary_line_for_message
from .loop_usage import _prompt_input_tokens_for_history

logger = logging.getLogger("monkeybot.core.runtime.loop.history_compaction")

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
- [answer-format / output contract from the user, verbatim when present \
(e.g. `Qxx:` / `Evidence:` lines); otherwise omit this bullet]

## Work State
### Completed
- [finished work, verified facts, or changes made; otherwise "(none)"]
- [explicit task-progress ranges when present, verbatim \
(e.g. "Q01–Q22 answered with evidence; Q23–Q48 remaining")]

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
- Preserve, verbatim, any explicit task-progress state and any answer-format \
contract stated by the user.
- Preserve tool-strategy commitments under Important Details or Work State \
(e.g. search-first for codebase Q&A; glob for locate-a-file / assets).
- Do not mention the summary process or that context was compacted.\
"""

# Appended to every compacted summary so search-first / format rules survive
# even when middle-history exemplars are dropped (post-summarization epoch).
# Keep this a short pointer — full rules live in the harness (avoid drift /
# duplicated "Standing instructions" blocks accumulating across compactions epochs).
_POST_COMPACTION_STANDING_HEADING = "## Standing instructions (still in effect after compaction)"
_POST_COMPACTION_STANDING = f"""\
{_POST_COMPACTION_STANDING_HEADING}
Harness rules still apply: prefer `search` before broad exploration for codebase Q&A; \
`read_file` (or `glob` for binary assets) before `Evidence:` citations; keep incremental \
answers in a workspace file for long multi-item tasks; preserve any user answer-format \
contract verbatim.\
"""


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
    if _POST_COMPACTION_STANDING_HEADING in summary_text:
        summary_body = f"[Context Summary]:\n{summary_text}"
    else:
        summary_body = f"[Context Summary]:\n{summary_text}\n\n{_POST_COMPACTION_STANDING}"
    merged = [
        *head,
        Message(
            role="assistant",
            content=[Text(text=summary_body)],
        ),
        *tail,
    ]
    await history.reset(thread_id, merged)
    return len(middle)
