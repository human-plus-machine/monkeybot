"""Hand the event loop back during a long pure-Python pass.

A thread does not help this work: it holds the GIL, so the loop waits for it
either way. Yielding on a time budget keeps each stretch short. The budget is
time, not a row count, because a row can be a few bytes or several megabytes.
One row larger than the budget cannot be split.
"""

from __future__ import annotations

import asyncio
import time


def _resume(done: asyncio.Future[None]) -> None:
    if not done.done():
        done.set_result(None)


class LoopYield:
    """Hand the event loop back once a pass has held it for ``budget_s``."""

    def __init__(self, budget_s: float = 0.002) -> None:
        self._budget = budget_s
        self._deadline = time.perf_counter() + budget_s

    async def tick(self) -> None:
        if time.perf_counter() < self._deadline:
            return
        # ``sleep(0)`` resumes this task before timers that already came due,
        # and the next chunk then blocks the loop before those timers run.
        # A zero-delay timer is polled with IO, so a waiting session runs first.
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        loop.call_later(0, _resume, done)
        await done
        self._deadline = time.perf_counter() + self._budget
