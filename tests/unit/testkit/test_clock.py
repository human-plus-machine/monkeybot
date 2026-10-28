"""FakeClock drives the scheduler, background summary, and stale-claim seams."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import Done, Message, TextDelta
from monkeybot.core.persistence.scheduled_loops import ScheduledLoopCreate
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.runtime import background_summary as bg
from monkeybot.core.runtime.background_summary import (
    maybe_start,
    reset_pending_summaries,
    wait_idle,
)
from monkeybot.core.runtime.context_budget import PRESSURE_MODERATE_RATIO, SUMMARY_TRIGGER_RATIO
from monkeybot.core.types.content_blocks import ContentBlock, SystemNotification, Text
from monkeybot.scheduler.engine import run_scheduler_loop
from monkeybot.scheduler.tick_result import TickInvokeResult
from testkit.clock import FakeClock
from testkit.history import FakeHistory
from testkit.provider import FakeProvider

_WINDOW = 10_000
_FLOOR = max(1, int(_WINDOW * PRESSURE_MODERATE_RATIO))
_CAP = max(1, int(_WINDOW * SUMMARY_TRIGGER_RATIO))


def test_advance_moves_now_and_rejects_going_backwards() -> None:
    clock = FakeClock(1_000)
    assert clock.now_ms() == 1_000
    assert clock.advance(0) == 1_000
    assert clock.advance(250) == 1_250
    assert clock.now_ms() == 1_250
    with pytest.raises(ValueError, match="ms must be >= 0"):
        clock.advance(-1)
    assert clock.now_ms() == 1_250


@dataclass
class _Busy:
    def is_busy(self, session_id: str) -> bool:
        del session_id
        return False


@dataclass
class _Ensurer:
    async def ensure_session(self, session_id: str) -> None:
        del session_id


@dataclass
class _Invoker:
    shutdown: asyncio.Event
    calls: list[str] = field(default_factory=list)
    fired: asyncio.Event = field(default_factory=asyncio.Event)

    async def invoke_tick(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> TickInvokeResult:
        del request_id, user_content
        self.calls.append(session_id)
        self.fired.set()
        self.shutdown.set()
        return TickInvokeResult.ok()


async def test_scheduler_fires_when_fake_clock_reaches_next_tick(tmp_path: object) -> None:
    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path}/clock-scheduler.db")
    await backend.open()
    task: asyncio.Task[None] | None = None
    release = asyncio.Event()
    shutdown = asyncio.Event()
    try:
        store = backend.scheduled_loops()
        row = await store.create(
            ScheduledLoopCreate(
                prompt="BUSINESS: tick",
                interval_ms=60_000,
                session_id="sess-1",
                loop_id="loop-1",
                max_ticks=5,
            )
        )
        clock = FakeClock(row.next_tick_at_ms - 1)
        seen: list[int] = []
        first = asyncio.Event()
        original = store.list_due

        async def list_due(now_ms: int) -> list[object]:
            seen.append(now_ms)
            if len(seen) == 1:
                first.set()
                await release.wait()
            return await original(now_ms)

        store.list_due = list_due  # type: ignore[method-assign]
        invoker = _Invoker(shutdown=shutdown)
        task = asyncio.create_task(
            run_scheduler_loop(
                store=store,
                invoker=invoker,
                session_busy=_Busy(),
                ensure_session=_Ensurer(),
                worker_id="worker-1",
                poll_interval_s=0,
                claim_heartbeat_interval_s=60,
                shutdown=shutdown,
                clock=clock.now_ms,
            )
        )
        await asyncio.wait_for(first.wait(), timeout=2)
        assert invoker.calls == []
        assert seen == [row.next_tick_at_ms - 1]
        clock.advance(1)
        release.set()
        await asyncio.wait_for(invoker.fired.wait(), timeout=2)
        assert invoker.calls == ["sess-1"]
        assert row.next_tick_at_ms in seen
        await asyncio.wait_for(task, timeout=2)
    finally:
        release.set()
        shutdown.set()
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await backend.close()


async def test_stale_claim_window_follows_fake_clock(tmp_path: object) -> None:
    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path}/clock-stale.db")
    await backend.open()
    try:
        store = backend.scheduled_loops()
        row = await store.create(
            ScheduledLoopCreate(
                prompt="BUSINESS: tick",
                interval_ms=60_000,
                session_id="sess-1",
                loop_id="loop-1",
                max_ticks=5,
            )
        )
        claimed = await store.claim_tick(row.loop_id, "worker-1", now_ms=row.next_tick_at_ms)
        assert claimed is not None
        assert claimed.claimed_at_ms == row.next_tick_at_ms
        clock = FakeClock(claimed.claimed_at_ms)
        assert await store.release_stale_claims(1_000, now_ms=clock.now_ms()) == 0
        clock.advance(1_000)
        # claimed_at < now - stale_after, so the exact boundary is still live.
        assert await store.release_stale_claims(1_000, now_ms=clock.now_ms()) == 0
        clock.advance(1)
        assert await store.release_stale_claims(1_000, now_ms=clock.now_ms()) == 1
        released = await store.get(row.loop_id)
        assert released is not None
        assert released.tick_in_flight is False
        assert released.claimed_at_ms is None
    finally:
        await backend.close()


def _summary_rows() -> list[Message]:
    rows = [
        Message(
            role="user" if i % 2 == 0 else "assistant",
            content=[Text(text=f"fact {i} " + "x" * 80)],
        )
        for i in range(8)
    ]
    rows.insert(
        1,
        Message(
            role="user",
            content=[SystemNotification(notification_type="verifierVerdict", msg="verdict-ok")],
        ),
    )
    return rows


def _summary_ctx() -> TurnContext:
    return TurnContext(
        thread_id="clock-summary",
        request_id="r1",
        agent_md="",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
        context_window_tokens=_WINDOW,
    )


@pytest.fixture
async def _clear_summaries() -> AsyncIterator[None]:
    await reset_pending_summaries()
    yield
    await reset_pending_summaries()


async def test_background_summary_scheduling_follows_fake_clock(
    _clear_summaries: None,
) -> None:
    del _clear_summaries
    assert _FLOOR < _CAP
    ctx = _summary_ctx()
    clock = FakeClock(0)
    history = FakeHistory(_summary_rows())
    provider = FakeProvider([[TextDelta(text="bg-summary"), Done()]])
    await maybe_start(ctx, history, provider, _FLOOR - 1, clock=clock.now_ms)  # type: ignore[arg-type]
    clock.advance(10_000_000)
    await maybe_start(ctx, history, provider, _FLOOR - 1, clock=clock.now_ms)  # type: ignore[arg-type]
    assert ctx.thread_id not in bg._pending
    assert provider.stream_calls == 0

    clock = FakeClock(5_000)
    await maybe_start(ctx, history, provider, _FLOOR, clock=clock.now_ms)  # type: ignore[arg-type]
    pending = bg._pending[ctx.thread_id]
    assert pending.started_at_ms == 5_000
    clock.advance(999_999)
    await maybe_start(ctx, history, provider, _FLOOR, clock=clock.now_ms)  # type: ignore[arg-type]
    assert bg._pending[ctx.thread_id] is pending
    assert pending.started_at_ms == 5_000
    await wait_idle(ctx.thread_id)

    before = int(time.time() * 1000)
    wall_ctx = TurnContext(
        thread_id="clock-summary-wall",
        request_id="r2",
        agent_md="",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
        context_window_tokens=_WINDOW,
    )
    await maybe_start(wall_ctx, history, provider, _FLOOR)  # type: ignore[arg-type]
    after = int(time.time() * 1000)
    assert before <= bg._pending[wall_ctx.thread_id].started_at_ms <= after
    await wait_idle(wall_ctx.thread_id)
