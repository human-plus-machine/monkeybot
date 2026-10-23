"""Prepare a thread summary before the turn that has to compact.

The job streams summary text only. It never writes history. The turn that crosses
the summary bar applies that text, or falls back to summarizing inline when the
job is missing, failed, or the covered rows have changed.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import Message, Provider
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.runtime.context_budget import PRESSURE_MODERATE_RATIO, SUMMARY_TRIGGER_RATIO

from .history_compaction import (
    SUMMARY_KEEP_TAIL_RATIO,
    _estimate_message_tokens,
    _finalize_summary_row,
    _generate_summary_text,
    _intent_facts_from_ctx,
    _summarization_model_id,
    _summarization_viable,
    rebuild_compacted_history,
    split_messages_for_compaction,
)

logger = logging.getLogger("monkeybot.core.runtime.background_summary")

# One in-flight summary per thread. Past this, the oldest job is cancelled.
_MAX_PENDING = 64


@dataclass
class PendingSummary:
    """A summary of ``covered`` that has not been written yet.

    ``cut`` is the index of the first tail row in the raw history the job saw.
    Rows appended after that stay verbatim when the summary is applied.
    """

    cut: int
    covered: list[Message]
    head: list[Message]
    middle: list[Message]
    model: str
    window_tokens: int
    task: asyncio.Task[str | None]


_pending: OrderedDict[str, PendingSummary] = OrderedDict()


def _tail_budget(window_tokens: int) -> int:
    return max(1, int(max(1, window_tokens) * SUMMARY_KEEP_TAIL_RATIO))


def _tail_tokens(messages: Sequence[Message]) -> int:
    return sum(_estimate_message_tokens(message) for message in messages)


def _first_tail_index(messages: Sequence[Message], tail: Sequence[Message]) -> int:
    """Raw index of the first tail row. Tail rows are the same objects ``split`` returned."""
    if not tail:
        return len(messages)
    first = tail[0]
    for index, message in enumerate(messages):
        if message is first:
            return index
    return len(messages)


def _prefix_matches(pending: PendingSummary, raw: Sequence[Message]) -> bool:
    if pending.cut > len(raw):
        return False
    return list(raw[: pending.cut]) == list(pending.covered)


def _is_fresh(pending: PendingSummary, raw: Sequence[Message], *, model: str, window: int) -> bool:
    """True when this job still covers the thread and its tail fits the keep budget.

    A finished job that produced no text is not fresh, so the next in-band turn
    can try again. A changed model, window, or covered prefix is not fresh either:
    applying it would summarize the wrong rows.
    """
    task = pending.task
    if task.done():
        if task.cancelled():
            return False
        try:
            if not task.result():
                return False
        except (asyncio.CancelledError, Exception):
            return False
    if pending.model != model or pending.window_tokens != window:
        return False
    if not _prefix_matches(pending, raw):
        return False
    return _tail_tokens(raw[pending.cut :]) <= _tail_budget(window)


def _retrieve(task: asyncio.Task[str | None]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        return


def _cancel(task: asyncio.Task[str | None]) -> None:
    if task.done():
        _retrieve(task)
        return
    task.add_done_callback(_retrieve)
    task.cancel()


def _drop(thread_id: str) -> None:
    pending = _pending.pop(thread_id, None)
    if pending is not None:
        _cancel(pending.task)


def _evict_until_room(thread_id: str) -> None:
    if thread_id in _pending:
        return
    while len(_pending) >= _MAX_PENDING:
        old_id, old = _pending.popitem(last=False)
        logger.debug("background summary evicted %s", kv(thread_id=old_id))
        _cancel(old.task)


async def _run_job(
    provider: Provider,
    model: str,
    middle: Sequence[Message],
    *,
    window_tokens: int,
    intent_facts: str | None,
    thread_id: str,
) -> str | None:
    try:
        return await _generate_summary_text(
            provider,
            model,
            middle,
            window_tokens=window_tokens,
            intent_facts=intent_facts,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "background summary failed %s",
            kv(thread_id=thread_id),
            exc_info=True,
        )
        return None


async def maybe_start(
    ctx: TurnContext,
    history: HistoryStore,
    provider: Provider,
    preflight: int,
) -> None:
    """Start a summary when the thread is past 70% of the window and under the bar.

    Returns once the model call is scheduled. The call itself is not awaited.
    A job that is still fresh is left alone. A job whose rows after the cut exceed
    the tail budget is replaced, so the summary still covers the thread.
    """
    window = ctx.context_window_tokens
    if window <= 0:
        return
    floor = max(1, int(window * PRESSURE_MODERATE_RATIO))
    cap = max(1, int(window * SUMMARY_TRIGGER_RATIO))
    if floor >= cap or preflight < floor or preflight >= cap:
        return
    raw = await history.load(ctx.thread_id)
    model = _summarization_model_id(ctx)
    existing = _pending.get(ctx.thread_id)
    if existing is not None and _is_fresh(existing, raw, model=model, window=window):
        return
    if not _summarization_viable(raw, window_tokens=window):
        return
    head, middle, tail = split_messages_for_compaction(raw, window_tokens=window)
    cut = _first_tail_index(raw, tail)
    if not tail or cut <= 0 or cut >= len(raw):
        return
    if existing is not None:
        _drop(ctx.thread_id)
    _evict_until_room(ctx.thread_id)
    task = asyncio.create_task(
        _run_job(
            provider,
            model,
            list(middle),
            window_tokens=window,
            intent_facts=_intent_facts_from_ctx(ctx),
            thread_id=ctx.thread_id,
        ),
        name=f"background-summary-{ctx.thread_id}",
    )
    _pending[ctx.thread_id] = PendingSummary(
        cut=cut,
        covered=list(raw[:cut]),
        head=list(head),
        middle=list(middle),
        model=model,
        window_tokens=window,
        task=task,
    )
    logger.debug(
        "background summary started %s",
        kv(thread_id=ctx.thread_id, cut=cut, middle=len(middle), preflight=preflight),
    )


async def take(
    thread_id: str,
    raw: Sequence[Message],
    *,
    model: str,
    window_tokens: int,
) -> tuple[PendingSummary, str] | None:
    """Pop this thread's job and return it when the covered prefix is unchanged.

    A running job is awaited. ``None`` means the caller should summarize inline.
    """
    pending = _pending.pop(thread_id, None)
    if pending is None:
        return None
    try:
        text = await pending.task
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "background summary failed %s",
            kv(thread_id=thread_id),
            exc_info=True,
        )
        return None
    if (
        not text
        or pending.model != model
        or pending.window_tokens != window_tokens
        or not _prefix_matches(pending, raw)
    ):
        logger.debug(
            "background summary discarded %s",
            kv(
                thread_id=thread_id,
                model_match=pending.model == model,
                window_match=pending.window_tokens == window_tokens,
                prefix_match=_prefix_matches(pending, raw),
            ),
        )
        return None
    return pending, text


async def apply_pending_summary(
    *,
    thread_id: str,
    history: HistoryStore,
    model: str,
    window_tokens: int,
    workspace_trailer: str | None,
) -> int | None:
    """Write a ready summary. ``None`` when there is nothing to apply.

    The trailer is read at apply time. Rows appended after the job's cut are kept,
    in order, after the compacted prefix.
    """
    if thread_id not in _pending:
        return None
    raw = await history.load(thread_id)
    got = await take(thread_id, raw, model=model, window_tokens=window_tokens)
    if got is None:
        return None
    pending, text = got
    if not pending.middle:
        return None
    summary = _finalize_summary_row(text, workspace_trailer)
    # Rebuild from the snapshotted objects: head matching is by identity, and a
    # fresh load() returns equal rows that are not those objects.
    merged = rebuild_compacted_history(
        pending.covered,
        head=pending.head,
        summary=summary,
        tail=[],
    ) + list(raw[pending.cut :])
    await history.reset(thread_id, merged)
    logger.debug(
        "background summary applied %s",
        kv(
            thread_id=thread_id,
            turns_summarized=len(pending.middle),
            kept_rows=len(merged),
        ),
    )
    return len(pending.middle)


async def wait_idle(thread_id: str) -> None:
    """Wait until this thread's job has finished, without taking it.

    The profiling spike uses this in place of the time a person spends between turns.
    Cancelling the waiter does not cancel the job.
    """
    pending = _pending.get(thread_id)
    if pending is None or pending.task.done():
        return
    task = pending.task
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        if task.cancelled() or task.done():
            return
        raise
    except Exception:
        return


async def reset_pending_summaries() -> None:
    """Cancel every job. Tests use this so one case cannot compact another's thread."""
    tasks = [pending.task for pending in _pending.values()]
    _pending.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
