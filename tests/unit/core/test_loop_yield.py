"""LoopYield hands the loop back on a time budget, and not before."""

from __future__ import annotations

import asyncio

from monkeybot.core.loop_yield import LoopYield


async def test_zero_budget_lets_another_task_run() -> None:
    pace = LoopYield(budget_s=0)
    order: list[str] = []

    async def other() -> None:
        order.append("other")

    task = asyncio.create_task(other())
    order.append("before")
    await pace.tick()
    order.append("after")
    await task

    assert order == ["before", "other", "after"]


async def test_large_budget_does_not_yield() -> None:
    pace = LoopYield(budget_s=10)
    order: list[str] = []

    async def other() -> None:
        order.append("other")

    task = asyncio.create_task(other())
    order.append("before")
    await pace.tick()
    order.append("after")

    assert order == ["before", "after"]
    await task
