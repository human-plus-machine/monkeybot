"""Scheduler engine cases driven by FakeClock (T2-SCHED-05..08).

``create`` stamps ``next_tick_at_ms`` from the wall clock. These tests place
the fake clock on that stamp and pass ``clock=`` into ``run_scheduler_loop``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest

from monkeybot.core.goals.service import DurableGoalService
from monkeybot.core.persistence.backends import ScheduledLoopStore
from monkeybot.core.persistence.scheduled_loops import (
    GOAL_MAX_CONSECUTIVE_ERRORS,
    ScheduledLoopCreate,
    ScheduledLoopRow,
    format_tick_prompt,
)
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.types.content_blocks import ContentBlock, Text
from monkeybot.scheduler.engine import SessionBusyChecker, TickInvoker, run_scheduler_loop
from monkeybot.scheduler.tick_result import TickInvokeResult
from testkit.clock import FakeClock

# Same default as ``run_scheduler_loop``'s ``stale_claim_ms`` parameter.
STALE_CLAIM_MS = 600_000

_TickCall = tuple[str, str, list[ContentBlock]]


@dataclass
class _Idle:
    def is_busy(self, session_id: str) -> bool:
        del session_id
        return False


@dataclass
class _AlwaysBusy:
    def is_busy(self, session_id: str) -> bool:
        del session_id
        return True


@dataclass
class _StealBusy:
    """Release the scheduler's claim and let another worker take it."""

    store: ScheduledLoopStore
    clock: FakeClock
    loop_id: str
    released: int | None = None
    stolen: bool | None = None

    def is_busy(self, session_id: str) -> bool:
        del session_id
        return True

    async def is_busy_async(self, session_id: str) -> bool:
        del session_id
        if self.released is None:
            self.clock.advance(1)
            self.released = await self.store.release_stale_claims(0, now_ms=self.clock.now_ms())
            stolen = await self.store.claim_tick(
                self.loop_id, "worker-b", now_ms=self.clock.now_ms()
            )
            self.stolen = stolen is not None
        return True


@dataclass
class _Ensurer:
    sessions: list[str] = field(default_factory=list)

    async def ensure_session(self, session_id: str) -> None:
        self.sessions.append(session_id)


@dataclass
class _RecordingInvoker:
    result: TickInvokeResult = field(default_factory=TickInvokeResult.ok)
    calls: list[_TickCall] = field(default_factory=list)

    async def invoke_tick(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> TickInvokeResult:
        self.calls.append((session_id, request_id, user_content))
        return self.result


@dataclass
class _HeartbeatProbe:
    """Jump the clock, then let the claim heartbeat renew before a stale check."""

    store: ScheduledLoopStore
    clock: FakeClock
    loop_id: str
    calls: int = 0
    released: int | None = None
    other_claimed: bool | None = None

    async def invoke_tick(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> TickInvokeResult:
        del session_id, request_id, user_content
        self.calls += 1
        self.clock.advance(STALE_CLAIM_MS + 1)
        await asyncio.sleep(0.2)
        self.released = await self.store.release_stale_claims(
            STALE_CLAIM_MS, now_ms=self.clock.now_ms()
        )
        other = await self.store.claim_tick(self.loop_id, "worker-b", now_ms=self.clock.now_ms())
        self.other_claimed = other is not None
        return TickInvokeResult.ok()


def _tick_text(call: _TickCall) -> str:
    block = call[2][0]
    assert isinstance(block, Text)
    return block.text


def _advance_until_due(clock: FakeClock, next_tick_at_ms: int) -> None:
    lag = next_tick_at_ms - clock.now_ms()
    if lag > 0:
        clock.advance(lag)


async def _wait_until(predicate: Callable[[], bool], label: str, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {label}")


async def _wait_row(
    store: ScheduledLoopStore,
    loop_id: str,
    predicate: Callable[[ScheduledLoopRow], bool],
    label: str,
    timeout: float = 3.0,
) -> ScheduledLoopRow:
    deadline = asyncio.get_running_loop().time() + timeout
    last: ScheduledLoopRow | None = None
    while asyncio.get_running_loop().time() < deadline:
        last = await store.get(loop_id)
        if last is not None and predicate(last):
            return last
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {label}; last={last!r}")


async def _create_loop(
    store: ScheduledLoopStore,
    *,
    loop_id: str,
    max_ticks: int = 5,
    skip_if_busy: bool = True,
    prompt: str = "BUSINESS: tick the deploy",
) -> ScheduledLoopRow:
    return await store.create(
        ScheduledLoopCreate(
            prompt=prompt,
            interval_ms=60_000,
            session_id="sess-1",
            loop_id=loop_id,
            max_ticks=max_ticks,
            skip_if_busy=skip_if_busy,
        )
    )


@asynccontextmanager
async def _running(
    store: ScheduledLoopStore,
    *,
    clock: FakeClock,
    invoker: TickInvoker,
    busy: SessionBusyChecker | None = None,
    worker_id: str = "worker-a",
    poll_interval_s: float = 0.01,
    stale_claim_ms: int = STALE_CLAIM_MS,
    claim_heartbeat_interval_s: float = 3600.0,
) -> AsyncIterator[None]:
    shutdown = asyncio.Event()
    task = asyncio.create_task(
        run_scheduler_loop(
            store=store,
            invoker=invoker,
            session_busy=busy if busy is not None else _Idle(),
            ensure_session=_Ensurer(),
            worker_id=worker_id,
            poll_interval_s=poll_interval_s,
            stale_claim_ms=stale_claim_ms,
            claim_heartbeat_interval_s=claim_heartbeat_interval_s,
            shutdown=shutdown,
            clock=clock.now_ms,
        )
    )
    try:
        yield
    finally:
        shutdown.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()
            await asyncio.wait({task})


@pytest.fixture
async def loop_store(tmp_path):
    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path / 'scheduler.db'}")
    await backend.open()
    try:
        yield backend.scheduled_loops()
    finally:
        await backend.close()


@pytest.mark.case("T2-SCHED-05")
async def test_one_tick_when_clock_passes_next_tick(loop_store: ScheduledLoopStore) -> None:
    row = await _create_loop(loop_store, loop_id="loop-one", max_ticks=1)
    clock = FakeClock(row.next_tick_at_ms - 1)
    invoker = _RecordingInvoker()
    async with _running(loop_store, clock=clock, invoker=invoker):
        await asyncio.sleep(0.05)
        assert invoker.calls == []
        clock.advance(1)
        await _wait_until(lambda: len(invoker.calls) >= 1, "one tick invoke")
        await _wait_row(
            loop_store,
            row.loop_id,
            lambda stored: stored.status == "completed" and stored.tick_index == 1,
            "loop completed after one tick",
        )
        assert len(invoker.calls) == 1
    assert len(invoker.calls) == 1
    assert invoker.calls[0][0] == row.session_id
    assert _tick_text(invoker.calls[0]) == format_tick_prompt(row)


@pytest.mark.case("T2-SCHED-06")
async def test_busy_session_defers_tick(loop_store: ScheduledLoopStore) -> None:
    row = await _create_loop(loop_store, loop_id="loop-busy", skip_if_busy=True)
    clock = FakeClock(row.next_tick_at_ms)
    invoker = _RecordingInvoker()
    async with _running(loop_store, clock=clock, invoker=invoker, busy=_AlwaysBusy()):
        updated = await _wait_row(
            loop_store,
            row.loop_id,
            lambda stored: (
                stored.last_error == "session busy; deferred" and not stored.tick_in_flight
            ),
            "busy session deferred",
        )
    assert invoker.calls == []
    assert updated.status == "active"
    assert updated.tick_index == 0
    assert updated.next_tick_at_ms == clock.now_ms() + row.interval_ms


@pytest.mark.case("T2-SCHED-06")
async def test_defer_tick_keeps_a_reclaimed_claim(loop_store: ScheduledLoopStore) -> None:
    row = await _create_loop(loop_store, loop_id="loop-reclaim", skip_if_busy=True)
    clock = FakeClock(row.next_tick_at_ms)
    invoker = _RecordingInvoker()
    busy = _StealBusy(store=loop_store, clock=clock, loop_id=row.loop_id)
    async with _running(loop_store, clock=clock, invoker=invoker, busy=busy, worker_id="worker-a"):
        await _wait_until(lambda: busy.stolen is True, "another worker reclaimed the claim")
        held = await loop_store.get(row.loop_id)
    assert busy.released == 1
    assert invoker.calls == []
    assert held is not None
    assert held.worker_id == "worker-b"
    assert held.tick_in_flight is True


@pytest.mark.case("T2-SCHED-07")
async def test_loop_tick_error_fails_immediately(loop_store: ScheduledLoopStore) -> None:
    row = await _create_loop(loop_store, loop_id="loop-fail")
    clock = FakeClock(row.next_tick_at_ms - 1)
    invoker = _RecordingInvoker(result=TickInvokeResult.fail("boom"))
    async with _running(loop_store, clock=clock, invoker=invoker):
        await asyncio.sleep(0.05)
        assert invoker.calls == []
        clock.advance(1)
        updated = await _wait_row(
            loop_store,
            row.loop_id,
            lambda stored: stored.status == "failed",
            "loop failed on tick error",
        )
    assert len(invoker.calls) == 1
    assert updated.stop_reason == "tick_error"
    assert updated.consecutive_error_count == 1


@pytest.mark.case("T2-SCHED-07")
async def test_goal_tick_errors_stay_active_until_third(loop_store: ScheduledLoopStore) -> None:
    assert GOAL_MAX_CONSECUTIVE_ERRORS == 3
    service = DurableGoalService(loop_store)
    created, was_new = await service.create(objective="Keep the deploy green", session_id="sess-1")
    assert was_new
    clock = FakeClock(created.next_tick_at_ms - 1)
    invoker = _RecordingInvoker(result=TickInvokeResult.fail("tick failed"))
    async with _running(loop_store, clock=clock, invoker=invoker):
        await asyncio.sleep(0.05)
        assert invoker.calls == []
        for expected in range(1, GOAL_MAX_CONSECUTIVE_ERRORS + 1):
            current = await loop_store.get(created.loop_id)
            assert current is not None
            _advance_until_due(clock, current.next_tick_at_ms)
            updated = await _wait_row(
                loop_store,
                created.loop_id,
                lambda stored, count=expected: stored.consecutive_error_count >= count,
                f"goal consecutive errors {expected}",
            )
            if expected < GOAL_MAX_CONSECUTIVE_ERRORS:
                assert updated.status == "active"
                assert updated.stop_reason is None
            else:
                assert updated.status == "failed"
                assert updated.stop_reason == "consecutive_tick_errors"
        assert len(invoker.calls) == GOAL_MAX_CONSECUTIVE_ERRORS
    assert len(invoker.calls) == GOAL_MAX_CONSECUTIVE_ERRORS


@pytest.mark.case("T2-SCHED-08")
async def test_stale_claim_is_reclaimed(loop_store: ScheduledLoopStore) -> None:
    row = await _create_loop(loop_store, loop_id="loop-stale")
    clock = FakeClock(row.next_tick_at_ms)
    held = await loop_store.claim_tick(row.loop_id, "worker-held", now_ms=clock.now_ms())
    assert held is not None
    invoker = _RecordingInvoker()
    async with _running(loop_store, clock=clock, invoker=invoker, worker_id="worker-b"):
        await asyncio.sleep(0.05)
        assert invoker.calls == []
        clock.advance(STALE_CLAIM_MS + 1)
        await _wait_until(lambda: len(invoker.calls) >= 1, "reclaimed tick")
        await _wait_row(
            loop_store,
            row.loop_id,
            lambda stored: stored.tick_index == 1,
            "reclaimed tick completed",
        )
        assert len(invoker.calls) == 1
    assert len(invoker.calls) == 1
    assert _tick_text(invoker.calls[0]) == format_tick_prompt(row)


@pytest.mark.case("T2-SCHED-08")
async def test_heartbeat_prevents_reclaim_during_slow_tick(loop_store: ScheduledLoopStore) -> None:
    row = await _create_loop(loop_store, loop_id="loop-heartbeat")
    clock = FakeClock(row.next_tick_at_ms)
    probe = _HeartbeatProbe(store=loop_store, clock=clock, loop_id=row.loop_id)
    async with _running(
        loop_store,
        clock=clock,
        invoker=probe,
        worker_id="worker-a",
        poll_interval_s=0.5,
        claim_heartbeat_interval_s=0.05,
    ):
        await _wait_until(lambda: probe.released is not None, "heartbeat stale check")
        assert probe.calls == 1
        assert probe.released == 0
        assert probe.other_claimed is False
        await _wait_row(
            loop_store,
            row.loop_id,
            lambda stored: stored.tick_index == 1 and not stored.tick_in_flight,
            "slow tick completed without reclaim",
        )
    assert probe.calls == 1
