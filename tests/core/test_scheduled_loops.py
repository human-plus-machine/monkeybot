"""Tests for scheduled loop persistence."""

from __future__ import annotations

import asyncio
import time

import pytest

from monkeybot.core.persistence.scheduled_loops import (
    ScheduledLoopCreate,
    _row_from_tuple,
    doc_to_scheduled_loop_row,
    format_tick_prompt,
    validate_loop_guards,
)
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend


@pytest.mark.asyncio
async def test_scheduled_loop_create_and_tick_lifecycle(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path / 'loops.db'}"
    backend = SQLiteStorageBackend(db_url)
    await backend.open()
    store = backend.scheduled_loops()
    row = await store.create(
        ScheduledLoopCreate(
            prompt="BUSINESS: append status",
            interval_ms=1000,
            session_id="loop-test",
            loop_id="demo",
            max_ticks=2,
        )
    )
    assert row.status == "active"
    assert row.tick_index == 0
    prompt = format_tick_prompt(row)
    assert "SCHEDULED TICK 1/2" in prompt
    assert "BUSINESS: append status" in prompt

    now_ms = int(time.time() * 1000)
    due = await store.list_due(now_ms + 5000)
    assert any(r.loop_id == "demo" for r in due)

    claimed = await store.claim_tick("demo", "worker-1")
    assert claimed is not None
    completed = await store.complete_tick("demo", worker_id="worker-1")
    assert completed is not None
    assert completed.tick_index == 1
    assert completed.status == "active"

    await asyncio.sleep(1.05)
    claimed2 = await store.claim_tick("demo", "worker-1")
    assert claimed2 is not None
    completed2 = await store.complete_tick("demo", worker_id="worker-1")
    assert completed2 is not None
    assert completed2.tick_index == 2
    assert completed2.status == "completed"
    assert completed2.stop_reason == "max_ticks"

    await backend.close()


@pytest.mark.asyncio
async def test_scheduled_loop_pause_resume_stop(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path / 'loops.db'}"
    backend = SQLiteStorageBackend(db_url)
    await backend.open()
    store = backend.scheduled_loops()
    await store.create(
        ScheduledLoopCreate(
            prompt="tick",
            interval_ms=5000,
            loop_id="ctrl",
            max_ticks=10,
        )
    )
    assert await store.pause("ctrl")
    row = await store.get("ctrl")
    assert row is not None and row.status == "paused"
    assert await store.resume("ctrl")
    row = await store.get("ctrl")
    assert row is not None and row.status == "active"
    assert await store.stop("ctrl")
    row = await store.get("ctrl")
    assert row is not None and row.status == "completed"
    await backend.close()


def test_validate_loop_guards_requires_stop_condition() -> None:
    with pytest.raises(ValueError, match="max_ticks, max_runtime, or unbounded"):
        validate_loop_guards(max_ticks=None, max_runtime_ms=None, unbounded=False)


def test_validate_loop_guards_allows_unbounded() -> None:
    validate_loop_guards(max_ticks=None, max_runtime_ms=None, unbounded=True)


def test_doc_to_scheduled_loop_row_roundtrip_fields() -> None:
    row = doc_to_scheduled_loop_row(
        "demo",
        {
            "session_id": "loop-main",
            "status": "active",
            "prompt": "tick plan",
            "interval_ms": 5000,
            "max_ticks": 3,
            "max_runtime_ms": 3600000,
            "skip_if_busy": 1,
            "tick_index": 2,
            "next_tick_at_ms": 100,
            "started_at_ms": 50,
            "last_tick_at_ms": 90,
            "last_error": None,
            "stop_reason": None,
            "tick_in_flight": 0,
            "worker_id": None,
            "claimed_at_ms": None,
        },
    )
    assert row.loop_id == "demo"
    assert row.session_id == "loop-main"
    assert row.max_ticks == 3
    assert row.skip_if_busy is True
    assert row.tick_index == 2


def test_doc_to_scheduled_loop_row_rejects_invalid_interval() -> None:
    with pytest.raises(ValueError, match="invalid interval_ms"):
        doc_to_scheduled_loop_row("bad", {"interval_ms": 0})


def _sql_loop_tuple(*, loop_id: str = "demo", interval_ms: int = 1000) -> tuple[object, ...]:
    return (
        loop_id,
        "loop-main",
        "active",
        "tick",
        interval_ms,
        10,
        None,
        1,
        0,
        100,
        50,
        None,
        None,
        None,
        0,
        None,
        None,
    )


def test_row_from_tuple_rejects_invalid_interval() -> None:
    with pytest.raises(ValueError, match="invalid interval_ms"):
        _row_from_tuple(_sql_loop_tuple(interval_ms=0))


def test_row_from_tuple_roundtrip_interval() -> None:
    row = _row_from_tuple(_sql_loop_tuple(interval_ms=5000))
    assert row.loop_id == "demo"
    assert row.interval_ms == 5000


@pytest.mark.asyncio
async def test_renew_tick_claim_prevents_stale_release(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path / 'loops.db'}"
    backend = SQLiteStorageBackend(db_url)
    await backend.open()
    store = backend.scheduled_loops()
    await store.create(
        ScheduledLoopCreate(
            prompt="tick",
            interval_ms=1000,
            loop_id="heartbeat",
            max_ticks=5,
        )
    )
    claimed = await store.claim_tick("heartbeat", "worker-1")
    assert claimed is not None
    assert await store.renew_tick_claim("heartbeat", "worker-1")
    released = await store.release_stale_claims(100)
    assert released == 0
    await backend.close()


@pytest.mark.asyncio
async def test_defer_tick_does_not_clear_reclaimed_claim(tmp_path) -> None:
    """Stale worker defer must not wipe a claim reclaimed by another worker."""
    db_url = f"sqlite:///{tmp_path / 'loops.db'}"
    backend = SQLiteStorageBackend(db_url)
    await backend.open()
    store = backend.scheduled_loops()
    await store.create(
        ScheduledLoopCreate(
            prompt="tick",
            interval_ms=1000,
            loop_id="race",
            max_ticks=5,
        )
    )
    claimed = await store.claim_tick("race", "worker-old")
    assert claimed is not None
    # Simulate lease expiry + reclaim by another worker.
    await store._conn.execute(
        "UPDATE scheduled_loops SET claimed_at_ms = 1 WHERE loop_id = ?",
        ("race",),
    )
    await store._conn.commit()
    released = await store.release_stale_claims(100)
    assert released == 1
    reclaimed = await store.claim_tick("race", "worker-new")
    assert reclaimed is not None
    assert reclaimed.worker_id == "worker-new"

    await store.defer_tick("race", worker_id="worker-old", reason="session busy")
    row = await store.get("race")
    assert row is not None
    assert row.tick_in_flight is True
    assert row.worker_id == "worker-new"
    await backend.close()


@pytest.mark.asyncio
async def test_defer_tick_releases_own_claim(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path / 'loops.db'}"
    backend = SQLiteStorageBackend(db_url)
    await backend.open()
    store = backend.scheduled_loops()
    await store.create(
        ScheduledLoopCreate(
            prompt="tick",
            interval_ms=5000,
            loop_id="defer-ok",
            max_ticks=5,
        )
    )
    claimed = await store.claim_tick("defer-ok", "worker-1")
    assert claimed is not None
    await store.defer_tick("defer-ok", worker_id="worker-1", reason="session busy")
    row = await store.get("defer-ok")
    assert row is not None
    assert row.tick_in_flight is False
    assert row.worker_id is None
    assert row.last_error == "session busy"
    await backend.close()


async def _insert_legacy_loop(
    store: object,
    *,
    loop_id: str,
    interval_ms: int,
    next_tick_at_ms: int = 0,
) -> None:
    conn = store._conn  # type: ignore[attr-defined]
    await conn.execute(
        """
        INSERT INTO scheduled_loops(
            loop_id, session_id, status, prompt, interval_ms,
            max_ticks, max_runtime_ms, skip_if_busy, tick_index,
            next_tick_at_ms, started_at_ms, last_tick_at_ms,
            last_error, stop_reason, tick_in_flight, worker_id, claimed_at_ms
        ) VALUES (?, 'loop-main', 'active', 'tick', ?, 10, NULL, 1, 0, ?, 0,
                  NULL, NULL, NULL, 0, NULL, NULL)
        """,
        (loop_id, interval_ms, next_tick_at_ms),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_sql_list_due_skips_malformed_interval(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path / 'loops.db'}"
    backend = SQLiteStorageBackend(db_url)
    await backend.open()
    store = backend.scheduled_loops()
    await store.create(
        ScheduledLoopCreate(
            prompt="tick",
            interval_ms=1000,
            loop_id="good",
            max_ticks=5,
        )
    )
    await _insert_legacy_loop(store, loop_id="bad", interval_ms=0)
    due = await store.list_due(int(time.time() * 1000) + 10_000)
    assert [row.loop_id for row in due] == ["good"]
    assert await store.get("bad") is None
    all_rows = await store.list_all()
    assert [row.loop_id for row in all_rows] == ["good"]
    await backend.close()
