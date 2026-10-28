"""Tests for the scheduled-loop poll-and-fire engine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from monkeybot.core.persistence.scheduled_loops import (
    KIND_GOAL,
    ScheduledLoopCreate,
    ScheduledLoopRow,
)
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.types.content_blocks import ContentBlock
from monkeybot.scheduler.engine import _execute_claimed_tick
from monkeybot.scheduler.tick_result import TickInvokeResult


@dataclass
class _BusyChecker:
    busy: bool = False

    def is_busy(self, session_id: str) -> bool:
        del session_id
        return self.busy


@dataclass
class _Ensurer:
    sessions: list[str] = field(default_factory=list)

    async def ensure_session(self, session_id: str) -> None:
        self.sessions.append(session_id)


@dataclass
class _RecordingInvoker:
    result: TickInvokeResult = field(default_factory=TickInvokeResult.ok)
    calls: list[tuple[str, str, list[ContentBlock]]] = field(default_factory=list)

    async def invoke_tick(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> TickInvokeResult:
        self.calls.append((session_id, request_id, user_content))
        return self.result


@pytest.fixture
async def loop_store(tmp_path):
    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path / 'engine.db'}")
    await backend.open()
    store = backend.scheduled_loops()
    yield store
    await backend.close()


async def _active_row(store, *, skip_if_busy: bool = True) -> ScheduledLoopRow:
    row = await store.create(
        ScheduledLoopCreate(
            prompt="BUSINESS: tick",
            interval_ms=1000,
            session_id="sess-1",
            loop_id="loop-1",
            max_ticks=5,
            skip_if_busy=skip_if_busy,
        )
    )
    claimed = await store.claim_tick(row.loop_id, "worker-1")
    assert claimed is not None
    return claimed


@pytest.mark.asyncio
async def test_execute_claimed_tick_defers_on_remote_busy(loop_store) -> None:
    row = await _active_row(loop_store, skip_if_busy=True)
    invoker = _RecordingInvoker(result=TickInvokeResult.session_busy())
    await _execute_claimed_tick(
        store=loop_store,
        invoker=invoker,
        session_busy=_BusyChecker(busy=False),
        ensure_session=_Ensurer(),
        row=row,
        worker_id="worker-1",
        claim_heartbeat_interval_s=60.0,
    )
    updated = await loop_store.get("loop-1")
    assert updated is not None
    assert updated.status == "active"
    assert updated.tick_index == 0
    assert updated.tick_in_flight is False
    assert updated.last_error == "session busy; deferred"


@pytest.mark.asyncio
async def test_execute_claimed_tick_defers_when_busy_and_queue_if_busy(loop_store) -> None:
    row = await _active_row(loop_store, skip_if_busy=False)
    invoker = _RecordingInvoker(result=TickInvokeResult.session_busy())
    await _execute_claimed_tick(
        store=loop_store,
        invoker=invoker,
        session_busy=_BusyChecker(busy=False),
        ensure_session=_Ensurer(),
        row=row,
        worker_id="worker-1",
        claim_heartbeat_interval_s=60.0,
    )
    updated = await loop_store.get("loop-1")
    assert updated is not None
    assert updated.status == "active"
    assert updated.tick_index == 0
    assert updated.last_error == "session busy; deferred"


@pytest.mark.asyncio
async def test_execute_claimed_tick_completes_on_success(loop_store) -> None:
    row = await _active_row(loop_store)
    invoker = _RecordingInvoker(result=TickInvokeResult.ok())
    await _execute_claimed_tick(
        store=loop_store,
        invoker=invoker,
        session_busy=_BusyChecker(busy=False),
        ensure_session=_Ensurer(),
        row=row,
        worker_id="worker-1",
        claim_heartbeat_interval_s=60.0,
    )
    updated = await loop_store.get("loop-1")
    assert updated is not None
    assert updated.tick_index == 1
    assert updated.status == "active"


@pytest.mark.asyncio
async def test_execute_claimed_tick_loop_error_fails(loop_store) -> None:
    row = await _active_row(loop_store)
    invoker = _RecordingInvoker(result=TickInvokeResult.fail("boom"))
    await _execute_claimed_tick(
        store=loop_store,
        invoker=invoker,
        session_busy=_BusyChecker(busy=False),
        ensure_session=_Ensurer(),
        row=row,
        worker_id="worker-1",
        claim_heartbeat_interval_s=60.0,
    )
    updated = await loop_store.get("loop-1")
    assert updated is not None
    assert updated.status == "failed"
    assert updated.stop_reason == "tick_error"


@pytest.mark.asyncio
async def test_execute_claimed_tick_goal_error_retries(loop_store) -> None:
    from monkeybot.core.goals.service import DurableGoalService

    service = DurableGoalService(loop_store)
    created, _ = await service.create(objective="Keep going", session_id="sess-1")
    await loop_store._conn.execute(
        "UPDATE scheduled_loops SET next_tick_at_ms = 1 WHERE loop_id = ?",
        (created.loop_id,),
    )
    await loop_store._conn.commit()
    claimed = await loop_store.claim_tick(created.loop_id, "worker-1")
    assert claimed is not None
    assert claimed.kind == KIND_GOAL
    invoker = _RecordingInvoker(result=TickInvokeResult.fail("timeout"))
    await _execute_claimed_tick(
        store=loop_store,
        invoker=invoker,
        session_busy=_BusyChecker(busy=False),
        ensure_session=_Ensurer(),
        row=claimed,
        worker_id="worker-1",
        claim_heartbeat_interval_s=60.0,
    )
    updated = await loop_store.get(created.loop_id)
    assert updated is not None
    assert updated.status == "active"
    assert updated.last_error == "timeout"
    assert updated.stop_reason is None


@pytest.mark.asyncio
async def test_execute_claimed_tick_renews_claim_during_slow_invoke(loop_store) -> None:
    row = await _active_row(loop_store)

    class _SlowInvoker:
        async def invoke_tick(
            self,
            session_id: str,
            request_id: str,
            user_content: list[ContentBlock],
        ) -> TickInvokeResult:
            del session_id, request_id, user_content
            await asyncio.sleep(0.15)
            return TickInvokeResult.ok()

    await _execute_claimed_tick(
        store=loop_store,
        invoker=_SlowInvoker(),
        session_busy=_BusyChecker(busy=False),
        ensure_session=_Ensurer(),
        row=row,
        worker_id="worker-1",
        claim_heartbeat_interval_s=0.05,
    )
    released = await loop_store.release_stale_claims(50)
    assert released == 0
    updated = await loop_store.get("loop-1")
    assert updated is not None
    assert updated.tick_index == 1
