"""History compaction / summarization and budgeted tool-response append."""

from __future__ import annotations

import asyncio
import json
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
    estimate_tokens_from_char_count,
)
from monkeybot.core.types.content_blocks import (
    AttachmentRef,
    ContentBlock,
    File,
    Image,
    RedactedThinking,
    Text,
    Thinking,
    ToolRequest,
    ToolResponse,
)

from .events import AgentEvent, ContextSummarized, ContextSummarizing, ContextUsage
from .loop_messages import _load_agent_chat_history, _summary_line_for_message
from .loop_usage import _prompt_input_tokens_for_history

logger = logging.getLogger("monkeybot.core.runtime.loop.history_compaction")

# Fixed harness policy — not env/YAML tunable.
# Pure bug guard, not a normal-flow limit: row count is otherwise unbounded and
# only the token-pressure trigger (SUMMARY_TRIGGER_RATIO) should ever compact
# history. This only fires if token-based summarization is somehow never
# reached (e.g. a stuck estimate) so it never presents an unbounded row count
# to the provider or persistence layer.
HISTORY_LOAD_MAX = 5000

# Always keep the oldest row (usually the original user goal).
SUMMARY_KEEP_HEAD_COUNT = 1
# Keep newest rows until they consume this fraction of the model context window.
SUMMARY_KEEP_TAIL_RATIO = 0.20
# Floor so a user/assistant (or tool) pair survives even on tiny windows.
SUMMARY_KEEP_TAIL_MIN = 2

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


def _raw_block_char_count(block: ContentBlock) -> int:
    """Untruncated content length of a single block for keep-budget sizing."""
    if isinstance(block, Text):
        return len(block.text)
    if isinstance(block, ToolRequest):
        return len(json.dumps(block.args, sort_keys=True))
    if isinstance(block, ToolResponse):
        return sum(_raw_block_char_count(b) for b in block.result)
    if isinstance(block, Image | File):
        return len(block.data) + len(block.mime_type)
    if isinstance(block, Thinking):
        return len(block.thinking) + len(block.signature)
    if isinstance(block, RedactedThinking):
        return len(block.data)
    if isinstance(block, AttachmentRef):
        return len(block.attachment_id) + len(block.mime_type)
    return len(type(block).__name__)


def _raw_message_char_count(message: Message) -> int:
    """Untruncated content length of ``message`` for sizing, not display.

    Deliberately does not go through ``_summary_line_for_message`` /
    ``summarize_tool_result_text``: those cap tool-result text (for the LLM
    summarization prompt) at a fixed char limit, which would make keep-budget
    decisions blind to large tool outputs — the exact payloads a
    token-budgeted tail exists to protect against.

    Also counts ``Image`` / ``File`` base64 payloads and ``Thinking`` traces at
    full length: falling back to ``type(block).__name__`` would report ~5–8
    chars for multi-kilobyte multimodal / reasoning blocks and blow past
    ``SUMMARY_KEEP_TAIL_RATIO``.
    """
    return sum(_raw_block_char_count(block) for block in message.content)


def _estimate_message_tokens(message: Message) -> int:
    """Cheap local estimate for keep-budget decisions (no provider round-trip)."""
    return estimate_tokens_from_char_count(_raw_message_char_count(message))


def split_messages_for_compaction(
    messages: Sequence[Message],
    *,
    window_tokens: int,
) -> tuple[list[Message], list[Message], list[Message]]:
    """Split history into (head, middle, tail) with a token-budgeted recent tail.

    Tail grows from the newest message until it reaches
    ``window_tokens * SUMMARY_KEEP_TAIL_RATIO`` (at least ``SUMMARY_KEEP_TAIL_MIN``
    rows), leaving ≥1 middle row whenever the transcript is long enough to compact.
    """
    n = len(messages)
    min_for_middle = SUMMARY_KEEP_HEAD_COUNT + SUMMARY_KEEP_TAIL_MIN + 1
    if n < min_for_middle:
        return list(messages), [], []

    head = list(messages[:SUMMARY_KEEP_HEAD_COUNT])
    tail_budget = max(1, int(max(1, window_tokens) * SUMMARY_KEEP_TAIL_RATIO))
    max_tail = n - SUMMARY_KEEP_HEAD_COUNT - 1  # leave ≥1 middle

    tail_rev: list[Message] = []
    used = 0
    for i in range(n - 1, SUMMARY_KEEP_HEAD_COUNT - 1, -1):
        if len(tail_rev) >= max_tail:
            break
        msg = messages[i]
        tok = _estimate_message_tokens(msg)
        if len(tail_rev) >= SUMMARY_KEEP_TAIL_MIN and used + tok > tail_budget:
            break
        tail_rev.append(msg)
        used += tok

    tail = list(reversed(tail_rev))
    middle = list(messages[SUMMARY_KEEP_HEAD_COUNT : n - len(tail)])
    return head, middle, tail


def protect_recent_count(
    messages: Sequence[Message],
    *,
    window_tokens: int,
) -> int:
    """How many newest rows pressure-shaping should leave verbatim."""
    _, _, tail = split_messages_for_compaction(messages, window_tokens=window_tokens)
    if tail:
        return len(tail)
    return min(len(messages), SUMMARY_KEEP_TAIL_MIN)


def _summarization_viable(
    messages: Sequence[Message],
    *,
    window_tokens: int,
) -> bool:
    _, middle, _ = split_messages_for_compaction(messages, window_tokens=window_tokens)
    return bool(middle)


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
    window_tokens: int,
) -> int:
    """Summarize middle history when tool results exhausted headroom."""
    # Compaction persists via history.reset; use unrepaired rows so synthetic
    # in-memory repairs are never written to the store.
    chat_messages = await history.load(thread_id)
    if not _summarization_viable(chat_messages, window_tokens=window_tokens):
        return 0
    return await _summarize_history(
        thread_id,
        chat_messages,
        history,
        provider,
        model,
        window_tokens=window_tokens,
    )


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
    current = budgeter.used_tokens
    if needs_compaction:
        yield ContextSummarizing(
            request_id=ctx.request_id,
            estimated_tokens=current,
            context_window_tokens=ctx.context_window_tokens,
        )
        try:
            turns_summarized = await _compact_history_if_needed(
                thread_id=ctx.thread_id,
                history=history,
                provider=provider,
                model=_summarization_model_id(ctx),
                window_tokens=ctx.context_window_tokens,
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
            current = post
    yield ContextUsage(
        request_id=ctx.request_id,
        estimated_tokens=current,
        context_window_tokens=ctx.context_window_tokens,
    )


async def _summarize_history(
    thread_id: str,
    messages: list[Message],
    history: HistoryStore,
    provider: Provider,
    model: str,
    *,
    window_tokens: int,
) -> int:
    """Compress middle history into one assistant summary row. Returns middle row count."""
    head, middle, tail = split_messages_for_compaction(
        messages, window_tokens=window_tokens
    )
    if not middle:
        return 0
    logger.debug(
        "history compaction split %s",
        kv(
            thread_id=thread_id,
            window_tokens=window_tokens,
            head=len(head),
            middle=len(middle),
            tail=len(tail),
            tail_budget=max(1, int(max(1, window_tokens) * SUMMARY_KEEP_TAIL_RATIO)),
        ),
    )
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
    logger.debug(
        "history compaction reset %s",
        kv(
            thread_id=thread_id,
            turns_summarized=len(middle),
            kept_rows=len(merged),
        ),
    )
    return len(middle)
