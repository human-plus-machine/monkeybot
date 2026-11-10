"""History compaction / summarization and budgeted tool-response append."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, cast

from monkeybot.core.attachments.text import parse_attachment_descriptor_text
from monkeybot.core.coding.verification_gate import last_proven_verification
from monkeybot.core.context import TurnContext
from monkeybot.core.context.epoch import ContextEpochTracker
from monkeybot.core.hooks import HookEvent, HookManager
from monkeybot.core.llm.context_overflow import is_context_overflow
from monkeybot.core.llm.provider import (
    Done,
    Message,
    Provider,
    TextDelta,
    ThinkingDelta,
    UsageEvent,
)
from monkeybot.core.llm.usage import Usage
from monkeybot.core.logging_utils import kv
from monkeybot.core.messages.tool_integrity import interrupted_tool_result_text
from monkeybot.core.messages.transform_context import is_provider_visible, provider_visible_blocks
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.persistence.thread_summary import CONTEXT_SUMMARY_PREFIX
from monkeybot.core.persistence.transcript import TranscriptWriter
from monkeybot.core.runtime.context_budget import (
    ContextBudgeter,
    _head_and_tail,
    estimate_tokens,
    estimate_tokens_from_char_count,
    summary_input_budget,
)
from monkeybot.core.types.content_blocks import (
    AttachmentRef,
    ContentBlock,
    File,
    Image,
    RedactedThinking,
    SystemNotification,
    Text,
    Thinking,
    ToolRequest,
    ToolResponse,
)

from .events import AgentEvent, ContextSummarized, ContextSummarizing, ContextUsage
from .loop_hooks import _HOOK_READ_TIMEOUT_S, _fire_hook
from .loop_messages import _load_agent_chat_history, _summary_line_for_message
from .loop_usage import _prompt_input_tokens_for_history

logger = logging.getLogger("monkeybot.core.runtime.loop.history_compaction")


@dataclass(frozen=True, slots=True)
class _SummaryTranscript:
    """The turn whose transcript should record the next summary model call."""

    writer: TranscriptWriter
    request_id: str
    inner_turn: int


_summary_transcript: ContextVar[_SummaryTranscript | None] = ContextVar(
    "monkeybot_summary_transcript",
    default=None,
)


def bind_summary_transcript(
    writer: TranscriptWriter, *, request_id: str, inner_turn: int
) -> Token[_SummaryTranscript | None]:
    """Record summary model calls on ``writer`` until the token is reset."""
    return _summary_transcript.set(
        _SummaryTranscript(writer=writer, request_id=request_id, inner_turn=inner_turn)
    )


def reset_summary_transcript(token: Token[_SummaryTranscript | None]) -> None:
    _summary_transcript.reset(token)


def _intent_facts_from_ctx(ctx: TurnContext) -> str | None:
    ledger = ctx.goal_ledger
    if ledger is None:
        return None
    try:
        return ledger.compaction_facts(ctx.thread_id)
    except Exception:
        logger.warning(
            "goal_ledger compaction facts failed %s",
            kv(thread_id=ctx.thread_id, request_id=ctx.request_id),
            exc_info=True,
        )
        return None


_WORKSPACE_STATE_HEADING = "## Workspace state (harness-generated)"
_TRAILER_MAX_FILES = 25


def _workspace_trailer_from_ctx(ctx: TurnContext) -> str | None:
    """Facts about the workspace the harness itself recorded — never LLM-written.

    A summary can misremember which files were touched or whether the tests passed; the
    ledger saw every write and every command, so state it from there. ``None`` outside
    coding mode (no ledger) or when nothing has been written or run yet.
    """
    ledger = ctx.read_ledger
    if ledger is None:
        return None
    lines: list[str] = []
    written = sorted(ledger.written)
    if written:
        shown = ", ".join(written[:_TRAILER_MAX_FILES])
        if len(written) > _TRAILER_MAX_FILES:
            shown += f", +{len(written) - _TRAILER_MAX_FILES} more"
        lines.append(f"- Files written this session: {shown}")
    config = ctx.config
    patterns = config.coding.verification_gate.verify_commands if config is not None else ()
    verify = last_proven_verification(ledger.recent_commands, patterns) if patterns else None
    if verify is not None:
        outcome = "passed" if verify.exit_code == 0 else "FAILED"
        lines.append(
            f"- Last verification run: `{verify.command}` → exit {verify.exit_code} ({outcome})"
        )
    if not lines:
        return None
    lines.append(
        "- File contents read before this point are no longer in context: "
        "`read_file` again before editing."
    )
    return "\n".join([_WORKSPACE_STATE_HEADING, *lines])


async def _fire_history_compacted(
    hook_manager: HookManager | None, ctx: TurnContext, *, turns_summarized: int
) -> None:
    """Tell hooks that older history was replaced by a summary (awaited, bounded, never raises).

    Reads are cleared here, before hooks run. The summary trailer has already been
    taken from the ledger's write and command records, which survive ``clear``.
    Subagents compact through this loop and have no hook manager, so forgetting
    reads cannot depend on ``ReadLedgerCompactionHook`` being registered. That
    hook still clears too; a second ``clear`` is a no-op.
    """
    if turns_summarized <= 0:
        return
    ledger = ctx.read_ledger
    if ledger is not None:
        ledger.clear()
    await _fire_hook(
        hook_manager,
        event=HookEvent.HISTORY_COMPACTED,
        ctx=ctx,
        timeout_s=_HOOK_READ_TIMEOUT_S,
        turns_summarized=turns_summarized,
    )


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

# Appended to every compacted summary so exploration / format rules survive
# even when middle-history exemplars are dropped (post-summarization epoch).
# Keep this a short pointer — full rules live in the harness (avoid drift /
# duplicated "Standing instructions" blocks accumulating across compactions epochs).
_POST_COMPACTION_STANDING_HEADING = "## Standing instructions (still in effect after compaction)"
_POST_COMPACTION_STANDING = f"""\
{_POST_COMPACTION_STANDING_HEADING}
Harness rules still apply: prefer `grep` / `glob` before broad exploration for codebase Q&A; \
`read_file` (or `glob` for binary assets) before `Evidence:` citations; keep incremental \
answers in a workspace file for long multi-item tasks; preserve any user answer-format \
contract verbatim. File contents seen before this point are gone from context: `read_file` \
again before editing.\
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

    Sizes only provider-visible blocks (stored UI blocks and completed-reply
    Thinking are stripped before the provider call, so they must not shrink the
    token-budgeted tail).

    Also counts ``Image`` / ``File`` base64 payloads and ``Thinking`` traces at
    full length: falling back to ``type(block).__name__`` would report ~5–8
    chars for multi-kilobyte multimodal / reasoning blocks and blow past
    ``SUMMARY_KEEP_TAIL_RATIO``.
    """
    return sum(_raw_block_char_count(block) for block in provider_visible_blocks(message))


def _estimate_message_tokens(message: Message) -> int:
    """Cheap local estimate for keep-budget decisions (no provider round-trip)."""
    return estimate_tokens_from_char_count(_raw_message_char_count(message))


_PINNED_NOTIFICATION_TYPES = frozenset({"verifierVerdict"})


def _is_pinned_block(block: ContentBlock) -> bool:
    """True for a block compaction must keep even when it sits in the summarized middle."""
    if isinstance(block, SystemNotification):
        return block.notification_type in _PINNED_NOTIFICATION_TYPES
    return isinstance(block, Text) and parse_attachment_descriptor_text(block.text) is not None


def _is_pinned_history_row(msg: Message) -> bool:
    """True when every block on the row must survive compaction.

    Pinned rows are verifier verdicts and frozen attachment-descriptor lines.
    Other ``SystemNotification`` kinds (thinking / inline / credits) stay compactable.
    A descriptor mixed into a normal text row is not pinned; the product stores
    each descriptor as its own line.
    """
    return bool(msg.content) and all(_is_pinned_block(block) for block in msg.content)


def _message_tool_ids(message: Message) -> tuple[set[str], set[str]]:
    requests: set[str] = set()
    responses: set[str] = set()
    for block in message.content:
        if isinstance(block, ToolRequest) and block.id:
            requests.add(block.id)
        elif isinstance(block, ToolResponse) and block.id and not _is_unpersisted_repair(block):
            responses.add(block.id)
    return requests, responses


def _is_unpersisted_repair(block: ToolResponse) -> bool:
    """True for the in-memory placeholder ``repair_tool_turn_integrity`` inserts.

    That row is not stored. Treating it as a tail response pulls its request out
    of the middle and makes a repaired view look unsplittable.
    """
    expected = interrupted_tool_result_text(block.tool_name)
    return block.is_error and any(
        isinstance(part, Text) and part.text == expected for part in block.result
    )


def _extend_tail_to_keep_tool_pairs(
    messages: Sequence[Message], tail: list[Message]
) -> list[Message]:
    """Include the tool request when the budget already kept its response.

    The minimum tail can fill up on a huge response plus a newer row, then stop
    before the request. That stores a response with no request. The request is
    small; the pair stays together in the tail.
    """
    if not tail:
        return tail
    start = len(messages) - len(tail)
    if start <= 0:
        return tail
    request_ids: set[str] = set()
    response_ids: set[str] = set()
    for message in tail:
        requests, responses = _message_tool_ids(message)
        request_ids |= requests
        response_ids |= responses
    missing = response_ids - request_ids
    if not missing:
        return tail
    found_at: int | None = None
    for index in range(start - 1, -1, -1):
        requests, _responses = _message_tool_ids(messages[index])
        if not requests & missing:
            continue
        missing -= requests
        found_at = index
        if not missing:
            break
    if found_at is None or missing:
        return tail
    return list(messages[found_at:])


def _split_accountable(
    messages: Sequence[Message],
    *,
    window_tokens: int,
) -> tuple[list[Message], list[Message], list[Message]]:
    """Split history into (head, middle, tail) with a token-budgeted recent tail.

    Head and tail slots (and the tail budget) are counted over rows with
    provider-visible content; UI-only / thinking-only raw rows ride along with
    their neighbours for free. Tail grows from the newest message until it reaches
    ``window_tokens * SUMMARY_KEEP_TAIL_RATIO`` (at least ``SUMMARY_KEEP_TAIL_MIN``
    visible rows), leaving ≥1 middle row whenever the transcript is long enough.
    """
    n = len(messages)
    visible = [is_provider_visible(m) for m in messages]
    min_for_middle = SUMMARY_KEEP_HEAD_COUNT + SUMMARY_KEEP_TAIL_MIN + 1
    if sum(visible) < min_for_middle:
        return list(messages), [], []

    head_end = 0
    seen = 0
    for i, is_visible in enumerate(visible):
        if is_visible:
            seen += 1
            if seen == SUMMARY_KEEP_HEAD_COUNT:
                head_end = i + 1
                break
    head = list(messages[:head_end])
    tail_budget = max(1, int(max(1, window_tokens) * SUMMARY_KEEP_TAIL_RATIO))
    max_tail = n - head_end - 1  # leave ≥1 middle

    tail_rev: list[Message] = []
    visible_in_tail = 0
    used = 0
    for i in range(n - 1, head_end - 1, -1):
        if len(tail_rev) >= max_tail:
            break
        msg = messages[i]
        if visible[i]:
            tok = _estimate_message_tokens(msg)
            if visible_in_tail >= SUMMARY_KEEP_TAIL_MIN and used + tok > tail_budget:
                break
            used += tok
            visible_in_tail += 1
        tail_rev.append(msg)

    tail = _extend_tail_to_keep_tool_pairs(messages, list(reversed(tail_rev)))
    tail_start = n - len(tail)
    if tail_start < head_end:
        head = list(messages[:tail_start])
        middle = []
    else:
        middle = list(messages[head_end:tail_start])
    return head, middle, tail


def split_messages_for_compaction(
    messages: Sequence[Message],
    *,
    window_tokens: int,
) -> tuple[list[Message], list[Message], list[Message]]:
    """Split accountable rows only; pinned notification rows are spliced back later."""
    accountable = [msg for msg in messages if not _is_pinned_history_row(msg)]
    return _split_accountable(accountable, window_tokens=window_tokens)


def rebuild_compacted_history(
    original: Sequence[Message],
    *,
    head: Sequence[Message],
    summary: Message,
    tail: Sequence[Message],
) -> list[Message]:
    """Keep pinned notification rows in original relative order around compacted rows."""
    head_ids = {id(msg) for msg in head}
    tail_ids = {id(msg) for msg in tail}
    result: list[Message] = []
    summary_placed = False
    for msg in original:
        if _is_pinned_history_row(msg):
            result.append(msg)
            continue
        if id(msg) in head_ids:
            result.append(msg)
            continue
        if id(msg) in tail_ids:
            if not summary_placed:
                result.append(summary)
                summary_placed = True
            result.append(msg)
            continue
        if not summary_placed:
            result.append(summary)
            summary_placed = True
    if not summary_placed:
        result.append(summary)
    return result


def protect_recent_count(
    messages: Sequence[Message],
    *,
    window_tokens: int,
) -> int:
    """How many newest *full-list* rows pressure-shaping should leave verbatim.

    Tail length is computed over accountable rows, then expanded to the original
    span so pinned verdicts interleaved in that tail do not shrink the window
    the caller applies as ``len(messages) - protect_recent``.
    """
    _, _, tail = split_messages_for_compaction(messages, window_tokens=window_tokens)
    if not tail:
        return min(len(messages), SUMMARY_KEEP_TAIL_MIN)
    tail_ids = {id(msg) for msg in tail}
    for i, msg in enumerate(messages):
        if id(msg) in tail_ids:
            return len(messages) - i
    return len(tail)


def truncate_history_preserving_pins(
    messages: Sequence[Message],
    *,
    max_rows: int,
) -> list[Message]:
    """Keep newest accountable rows plus pinned verdicts, in original order."""
    if max_rows <= 0:
        return []
    if len(messages) <= max_rows:
        return list(messages)
    pinned = [msg for msg in messages if _is_pinned_history_row(msg)]
    accountable = [msg for msg in messages if not _is_pinned_history_row(msg)]
    keep_accountable_n = max(0, max_rows - len(pinned))
    keep_accountable = accountable[-keep_accountable_n:] if keep_accountable_n else []
    keep_ids = {id(msg) for msg in keep_accountable}
    keep_ids.update(id(msg) for msg in pinned)
    kept = [msg for msg in messages if id(msg) in keep_ids]
    if len(kept) <= max_rows:
        return kept
    overflow = len(kept) - max_rows
    pinned_ids = {id(msg) for msg in pinned}
    dropped = 0
    trimmed: list[Message] = []
    for msg in kept:
        if dropped < overflow and id(msg) in pinned_ids:
            dropped += 1
            continue
        trimmed.append(msg)
    return trimmed


def _middle_summary_lines(middle: Sequence[Message], *, window_tokens: int) -> list[str]:
    return [
        line for m in middle if (line := _summary_line_for_message(m, window_tokens=window_tokens))
    ]


def _summarization_viable(
    messages: Sequence[Message],
    *,
    window_tokens: int,
) -> bool:
    """True when ``_summarize_history`` on these raw rows would compact something."""
    _, middle, _ = split_messages_for_compaction(messages, window_tokens=window_tokens)
    return bool(_middle_summary_lines(middle, window_tokens=window_tokens))


async def _compact_history_if_needed(
    *,
    thread_id: str,
    history: HistoryStore,
    provider: Provider,
    model: str,
    window_tokens: int,
    intent_facts: str | None = None,
    workspace_trailer: str | None = None,
) -> int:
    """Summarize middle history when the thread has exhausted headroom.

    A summary prepared earlier in the background is applied first. A miss, a changed
    prefix, a tail that has grown past the rows that summary can keep, or a failed
    job falls through to the inline summarizer.
    """
    # Lazy import: background_summary imports this module's split and prompt helpers.
    from .background_summary import apply_pending_summary

    applied = await apply_pending_summary(
        thread_id=thread_id,
        history=history,
        model=model,
        window_tokens=window_tokens,
        workspace_trailer=workspace_trailer,
    )
    if applied is not None:
        return applied
    return await _summarize_history(
        thread_id,
        history,
        provider,
        model,
        window_tokens=window_tokens,
        intent_facts=intent_facts,
        workspace_trailer=workspace_trailer,
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
    hook_manager: HookManager | None = None,
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
                model=ctx.model,
                window_tokens=ctx.context_window_tokens,
                intent_facts=_intent_facts_from_ctx(ctx),
                workspace_trailer=_workspace_trailer_from_ctx(ctx),
            )
        except Exception:
            logger.warning(
                "post-tool summarization failed %s; continuing",
                kv(request_id=ctx.request_id, thread_id=ctx.thread_id),
                exc_info=True,
            )
            turns_summarized = 0
        await _fire_history_compacted(hook_manager, ctx, turns_summarized=turns_summarized)
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


_SUMMARY_LEAD = "Create a structured summary from the conversation segment below.\n\n"
_SUMMARY_SEPARATOR = "\n\n---\n\n"
# Extra attempts after a provider size rejection, for the whole summary.
_MAX_SUMMARY_HALVINGS = 3


def _summary_user_text(lines: Sequence[str], intent_facts: str | None) -> str:
    """The user message for one summary call. One chunk is the old single prompt."""
    user_text = _SUMMARY_LEAD + _SUMMARY_SEPARATOR.join(lines)
    if intent_facts:
        user_text = intent_facts + "\n\n" + user_text
    return user_text


def _summary_request_tokens(lines: Sequence[str], intent_facts: str | None) -> int:
    return estimate_tokens(_COMPACTION_SUMMARY_SYSTEM) + estimate_tokens(
        _summary_user_text(lines, intent_facts)
    )


def _summary_fits(lines: Sequence[str], intent_facts: str | None, budget: int) -> bool:
    return _summary_request_tokens(lines, intent_facts) <= budget


def _trim_line_to_budget(
    prefix: Sequence[str],
    line: str,
    *,
    budget: int,
    intent_facts: str | None,
) -> str:
    """Shorten ``line`` until it fits after ``prefix``, keeping its head and tail."""
    if _summary_fits([*prefix, line], intent_facts, budget):
        return line
    lo = 1
    hi = len(line)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        trimmed = _head_and_tail(line, mid)
        if trimmed and _summary_fits([*prefix, trimmed], intent_facts, budget):
            best = trimmed
            lo = mid + 1
        else:
            hi = mid - 1
    if best:
        return best
    # The fixed prompt already fills the budget. One character keeps the fold moving.
    return line[:1] or line


def _carry_line(summary: str) -> str:
    """Previous summary, in the same shape a stored summary row is re-summarized."""
    return f"assistant: {CONTEXT_SUMMARY_PREFIX}\n{summary}"


def _pack_summary_chunk(
    lines: list[str],
    index: int,
    *,
    carry: str | None,
    budget: int,
    intent_facts: str | None,
) -> tuple[list[str], int]:
    """Lines for one summary call, and the index of the first line not taken.

    ``carry`` is the summary of the lines already consumed. A line that does not
    fit in a call by itself is trimmed, so the index always advances.
    """
    chunk: list[str] = []
    if carry:
        chunk.append(_carry_line(carry))
        if not _summary_fits(chunk, intent_facts, budget):
            chunk[0] = _trim_line_to_budget([], chunk[0], budget=budget, intent_facts=intent_facts)
    start = index
    while index < len(lines):
        if _summary_fits([*chunk, lines[index]], intent_facts, budget):
            chunk.append(lines[index])
            index += 1
            continue
        if index > start:
            break
        lines[index] = _trim_line_to_budget(
            chunk, lines[index], budget=budget, intent_facts=intent_facts
        )
        chunk.append(lines[index])
        index += 1
        break
    if index == start:
        raise RuntimeError("summary packing did not advance")
    return chunk, index


async def _generate_summary_text(
    provider: Provider,
    model: str,
    middle: Sequence[Message],
    *,
    window_tokens: int,
    intent_facts: str | None = None,
) -> str | None:
    """Stream a summary of ``middle``. ``None`` when those rows have nothing to compact.

    The middle is summarized in calls that each fit ``summary_input_budget``. A
    middle that fits is one call, with the same prompt as before. A larger middle
    is folded: each later call starts from the summary so far. A provider size
    rejection halves that call and packs it again, at most three times for the
    whole summary. The workspace trailer is not applied here. It is taken from
    the ledger when the summary is written, so files touched after this call
    still appear.
    """
    source = _middle_summary_lines(middle, window_tokens=window_tokens)
    if not source:
        return None
    budget = summary_input_budget(window_tokens)
    summary: str | None = None
    index = 0
    halvings = 0
    while index < len(source):
        call_budget = budget
        while True:
            # Pack from a copy so a rejected attempt's trim does not stick.
            working = list(source)
            chunk, next_index = _pack_summary_chunk(
                working,
                index,
                carry=summary,
                budget=call_budget,
                intent_facts=intent_facts,
            )
            try:
                summary = await _summarize_chunk(provider, model, chunk, intent_facts=intent_facts)
            except Exception as exc:
                if not is_context_overflow(exc) or halvings >= _MAX_SUMMARY_HALVINGS:
                    raise
                halvings += 1
                call_budget = max(1, call_budget // 2)
                logger.warning(
                    "summary call exceeded the window; halving the chunk %s",
                    kv(halving=halvings, budget=call_budget),
                )
                continue
            index = next_index
            break
    return summary


async def _summarize_chunk(
    provider: Provider,
    model: str,
    lines: Sequence[str],
    *,
    intent_facts: str | None,
) -> str:
    """One summary call. The caller keeps the returned text only if every call succeeds."""
    summarize_messages = [
        Message(
            role="system",
            content=[Text(text=_COMPACTION_SUMMARY_SYSTEM)],
        ),
        Message(
            role="user",
            content=[Text(text=_summary_user_text(lines, intent_facts))],
        ),
    ]
    binding = _summary_transcript.get()
    exchange_id = uuid.uuid4().hex if binding is not None else None
    summary_text = ""
    thinking_text = ""
    usage: dict[str, object] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    recorded = False
    try:
        if binding is not None and exchange_id is not None:
            # Written before stream() is entered. The write's await is the last
            # yield before iteration starts, so this record sits at the index
            # replay consumes for this call.
            await binding.writer.write_provider_request(
                request_id=binding.request_id,
                inner_turn=binding.inner_turn,
                model=model,
                messages=[message.to_dict() for message in summarize_messages],
                tools=[],
                thinking_budget=None,
                exchange_id=exchange_id,
            )
            recorded = True
        summary_text, thinking_text, usage = await _stream_summary(
            provider, summarize_messages, model=model
        )
        return summary_text.strip() or "(empty summary)"
    finally:
        if recorded and binding is not None and exchange_id is not None:
            await binding.writer.write_provider_response(
                request_id=binding.request_id,
                inner_turn=binding.inner_turn,
                model=model,
                text=summary_text,
                thinking=thinking_text,
                tool_requests=[],
                usage=usage,
                exchange_id=exchange_id,
            )


async def _stream_summary(
    provider: Provider,
    messages: Sequence[Message],
    *,
    model: str,
) -> tuple[str, str, dict[str, object]]:
    """Read one summary stream. The raw text is returned before any stored-row rewrite."""
    summary_text = ""
    thinking_text = ""
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    cache_read_tokens = 0
    cache_creation_tokens = 0
    stream = cast(Any, provider.stream(messages, [], model=model))
    try:
        async for ev in stream:
            if isinstance(ev, TextDelta):
                summary_text += ev.text
            elif isinstance(ev, ThinkingDelta):
                thinking_text += ev.text
            elif isinstance(ev, UsageEvent):
                input_tokens += ev.input_tokens
                output_tokens += ev.output_tokens
                cached_tokens += ev.cached_tokens
                cache_read_tokens += ev.cache_read_tokens
                cache_creation_tokens += ev.cache_creation_tokens
            elif isinstance(ev, Done):
                break
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()
    return (
        summary_text,
        thinking_text,
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
        },
    )


def _finalize_summary_row(text: str, workspace_trailer: str | None) -> Message:
    """Build the stored summary row, including the apply-time workspace trailer."""
    summary_text = text.strip() or "(empty summary)"
    if workspace_trailer:
        # A prior compaction's trailer may have been echoed into the new summary; ours replaces it.
        summary_text = summary_text.split(_WORKSPACE_STATE_HEADING)[0].rstrip() or "(empty summary)"
        summary_text = f"{summary_text}\n\n{workspace_trailer}"
    if _POST_COMPACTION_STANDING_HEADING in summary_text:
        summary_body = f"{CONTEXT_SUMMARY_PREFIX}\n{summary_text}"
    else:
        summary_body = f"{CONTEXT_SUMMARY_PREFIX}\n{summary_text}\n\n{_POST_COMPACTION_STANDING}"
    return Message(
        role="assistant",
        content=[Text(text=summary_body)],
    )


async def _summarize_history(
    thread_id: str,
    history: HistoryStore,
    provider: Provider,
    model: str,
    *,
    window_tokens: int,
    intent_facts: str | None = None,
    workspace_trailer: str | None = None,
) -> int:
    """Compress middle history into one assistant summary row. Returns middle row count.

    Loads raw stored rows itself: ``history.reset`` persists whatever it is given, so
    callers must never hand it a ``transform_context`` view (that would permanently
    drop verdicts, descriptors, thinking and merged ``row_id``s, and persist
    synthetic tool repairs).
    """
    messages = await history.load(thread_id)
    head, middle, tail = split_messages_for_compaction(messages, window_tokens=window_tokens)
    if not _middle_summary_lines(middle, window_tokens=window_tokens):
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
    summary_text = await _generate_summary_text(
        provider,
        model,
        middle,
        window_tokens=window_tokens,
        intent_facts=intent_facts,
    )
    if not summary_text:
        return 0
    summary_row = _finalize_summary_row(summary_text, workspace_trailer)
    merged = rebuild_compacted_history(messages, head=head, summary=summary_row, tail=tail)
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
