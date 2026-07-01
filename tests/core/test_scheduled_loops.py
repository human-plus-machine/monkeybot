"""Tests for scheduled loop persistence."""

from __future__ import annotations

import asyncio
import time

import pytest

from monkeybot.core.persistence.scheduled_loops import ScheduledLoopCreate, format_tick_prompt
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
