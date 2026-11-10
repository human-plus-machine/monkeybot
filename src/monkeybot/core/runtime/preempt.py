"""Abort the current inner step so a promoted steer can run immediately."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from typing import Any, TypeVar

T = TypeVar("T")


class StepPreemptedError(Exception):
    """The current step was aborted because the user sent a message now."""


class StreamStalledError(TimeoutError):
    """No stream event arrived within the idle timeout."""

    def __init__(self, idle_timeout_s: float) -> None:
        super().__init__(f"Provider stream stalled: no event for {idle_timeout_s:g}s")
        self.idle_timeout_s = idle_timeout_s


async def race_preempt(
    awaitable: Awaitable[T],
    preempt: asyncio.Event | None,
    *,
    cancelled: asyncio.Event | None = None,
    idle_timeout_s: float | None = None,
) -> T:
    """Return ``awaitable``'s result, or abort it when ``preempt`` is set.

    Stop wins. When ``cancelled`` is set, the awaitable is abandoned and
    ``CancelledError`` is raised so the existing cancel path ends the turn.
    A preempt that fires first abandons the awaitable and raises
    :class:`StepPreemptedError`. A stream that already ended
    (``StopAsyncIteration``) is returned as-is so a finished step is not
    thrown away. ``idle_timeout_s`` aborts a wait that produces nothing and
    raises :class:`StreamStalledError`; it never outranks Stop or a steer.
    Zero or negative disables it.
    """
    if idle_timeout_s is not None and idle_timeout_s <= 0:
        idle_timeout_s = None
    task: asyncio.Task[T] = asyncio.ensure_future(awaitable)
    if cancelled is not None and cancelled.is_set():
        await _abandon(task)
        raise asyncio.CancelledError
    if preempt is None and cancelled is None and idle_timeout_s is None:
        return await task

    preempt_wait = asyncio.create_task(preempt.wait()) if preempt is not None else None
    cancel_wait = asyncio.create_task(cancelled.wait()) if cancelled is not None else None
    waiters: set[asyncio.Future[Any]] = {task}
    if preempt_wait is not None:
        waiters.add(preempt_wait)
    if cancel_wait is not None:
        waiters.add(cancel_wait)
    try:
        done, _pending = await asyncio.wait(
            waiters,
            return_when=asyncio.FIRST_COMPLETED,
            timeout=idle_timeout_s,
        )
        if not done:
            await _abandon(task)
            raise StreamStalledError(idle_timeout_s or 0)
        if _stream_ended(task):
            return task.result()
        if cancelled is not None and cancelled.is_set():
            await _abandon(task)
            raise asyncio.CancelledError
        if preempt is not None and preempt.is_set():
            await _abandon(task)
            raise StepPreemptedError
        return task.result()
    finally:
        for waiter in (preempt_wait, cancel_wait):
            if waiter is not None and not waiter.done():
                waiter.cancel()
        for waiter in (preempt_wait, cancel_wait):
            if waiter is None:
                continue
            with contextlib.suppress(asyncio.CancelledError):
                await waiter


def _stream_ended(task: asyncio.Task[object]) -> bool:
    if not task.done() or task.cancelled():
        return False
    return isinstance(task.exception(), StopAsyncIteration)


async def _abandon(task: asyncio.Task[object]) -> None:
    """Cancel unfinished work and retrieve any result so it is not logged later."""
    if not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return
    if not task.cancelled():
        task.exception()
