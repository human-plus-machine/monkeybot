"""Prepare a thread summary before the turn that has to compact.

The job streams summary text only. It never writes history. The turn that crosses
the summary bar applies that text, or falls back to summarizing inline when the
job is missing, failed, or the covered rows have changed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
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
    # ``len(raw)`` when the job started. A shorter history deleted the tail this
    # cut was built for (rewind, truncate); that job must not be applied.
    base_len: int
    # Tokens after ``cut`` when the job was started. A later suffix may grow up
    # to the tail budget, but not past the size this job already accepted.
    suffix_tokens: int
    # Epoch ms when the job was scheduled. Freshness does not read this.
    started_at_ms: int
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


def _suffix_within_limit(pending: PendingSummary, raw: Sequence[Message]) -> bool:
    """True when rows after the cut have not outgrown the tail this job can keep.

    The split keeps at least two recent rows even when they already exceed the
    token budget. That tail is fresh until new rows push it further: restarting
    on every in-band turn would cancel the same summary before it finishes.
    A tail that fit the budget may still grow up to the budget. A shorter
    history has lost rows, so this cut is no longer the one a new split would use.
    """
    if len(raw) < pending.base_len or pending.cut > len(raw):
        return False
    limit = max(pending.suffix_tokens, _tail_budget(pending.window_tokens))
    return _tail_tokens(raw[pending.cut :]) <= limit


def _is_fresh(pending: PendingSummary, raw: Sequence[Message], *, model: str, window: int) -> bool:
    """True when this job still covers the prefix it summarized and its tail still fits.

    A finished job that produced no text is not fresh, so the next in-band turn
    can try again. A changed model, window, or covered prefix is not fresh either.
    Rows after the cut may grow up to the tail budget (or up to the tail this job
    already accepted, when that was larger). Past that limit, or once rows have
    been deleted, the cut is stale: applying it would keep rows that now have to
    be summarized, or summarize rows a rewind meant to keep.
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
    return _suffix_within_limit(pending, raw)


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


def _drop_if_same(thread_id: str, pending: PendingSummary) -> None:
    """Remove ``pending`` only when it is still this thread's job."""
    if _pending.get(thread_id) is pending:
        _drop(thread_id)


def _waiter_cancelled() -> bool:
    current = asyncio.current_task()
    return current is not None and current.cancelling() > 0


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
    # Lazy import: loading providers pulls every SDK adapter, and this module
    # is imported on the turn path. The suppress call is the only use.
    from monkeybot.providers._stream_marks import suppress_stream_marks

    try:
        with suppress_stream_marks():
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
    *,
    clock: Callable[[], int] | None = None,
) -> None:
    """Start a summary when the thread is past 70% of the window and under the bar.

    Returns once the model call is scheduled. The call itself is not awaited.
    A job that is still fresh is left alone. A job whose rows after the cut have
    outgrown the tail it can keep is replaced, so the summary still covers the thread.
    ``clock`` stamps when the job was scheduled. Omit it to use the wall clock.
    The token band decides whether a job starts.
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
    started_at_ms = int(time.time() * 1000) if clock is None else clock()
    _pending[ctx.thread_id] = PendingSummary(
        cut=cut,
        covered=list(raw[:cut]),
        head=list(head),
        middle=list(middle),
        model=model,
        window_tokens=window,
        base_len=len(raw),
        suffix_tokens=_tail_tokens(raw[cut:]),
        started_at_ms=started_at_ms,
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
    """Return this thread's job when it still covers ``raw``.

    The job stays registered. The caller pops it only after a second check,
    immediately before writing history, so a cancelled turn does not lose a
    summary that is still running. A stale job is removed here and is not
    awaited: waiting would make the pressured turn slower than summarizing inline.
    ``None`` means the caller should summarize inline.
    """
    pending = _pending.get(thread_id)
    if pending is None:
        return None
    if not _is_fresh(pending, raw, model=model, window=window_tokens):
        logger.debug(
            "background summary discarded %s",
            kv(
                thread_id=thread_id,
                model_match=pending.model == model,
                window_match=pending.window_tokens == window_tokens,
                prefix_match=_prefix_matches(pending, raw),
                suffix_ok=_suffix_within_limit(pending, raw),
            ),
        )
        _drop_if_same(thread_id, pending)
        return None
    try:
        text = await pending.task
    except asyncio.CancelledError:
        if _waiter_cancelled():
            raise
        logger.debug(
            "background summary cancelled %s",
            kv(thread_id=thread_id),
        )
        _drop_if_same(thread_id, pending)
        return None
    except Exception:
        logger.warning(
            "background summary failed %s",
            kv(thread_id=thread_id),
            exc_info=True,
        )
        _drop_if_same(thread_id, pending)
        return None
    if not text or _pending.get(thread_id) is not pending:
        logger.debug(
            "background summary discarded %s",
            kv(thread_id=thread_id, empty=not text),
        )
        _drop_if_same(thread_id, pending)
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
    in order, after the compacted prefix, when they still fit the tail this job
    can keep. History is loaded again after the summary text is ready so a row
    that arrived while it was streaming is kept or forces an inline summary.
    """
    if thread_id not in _pending:
        return None
    raw = await history.load(thread_id)
    got = await take(thread_id, raw, model=model, window_tokens=window_tokens)
    if got is None:
        return None
    pending, text = got
    if not pending.middle:
        _drop_if_same(thread_id, pending)
        return None
    current = await history.load(thread_id)
    if _pending.get(thread_id) is not pending or not _is_fresh(
        pending, current, model=model, window=window_tokens
    ):
        # A replacement job belongs to whoever stored it. A stale one must not
        # be written over rows that have since outgrown its cut.
        if _pending.get(thread_id) is pending:
            _drop(thread_id)
        return None
    _pending.pop(thread_id, None)
    summary = _finalize_summary_row(text, workspace_trailer)
    # Rebuild from the snapshotted objects: head matching is by identity, and a
    # fresh load() returns equal rows that are not those objects. The suffix is
    # the post-await load, which includes rows appended while the job streamed.
    merged = rebuild_compacted_history(
        pending.covered,
        head=pending.head,
        summary=summary,
        tail=[],
    ) + list(current[pending.cut :])
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
        # The job finishing is not the waiter finishing. A cancelled waiter must
        # stay cancelled even when the job happens to end on the same tick.
        if _waiter_cancelled():
            raise
        return
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
