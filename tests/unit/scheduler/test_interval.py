"""Tests for scheduler interval parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.persistence.scheduled_loops import (
    ScheduledLoopCreate,
    SQLiteScheduledLoopStore,
)
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.scheduler.interval import parse_interval_ms, parse_optional_duration_ms


def test_parse_interval_seconds() -> None:
    assert parse_interval_ms("20s") == 20_000
    assert parse_interval_ms("5m") == 300_000
    assert parse_interval_ms("1h") == 3_600_000
    assert parse_interval_ms(30) == 30_000


def test_parse_optional_duration() -> None:
    assert parse_optional_duration_ms(None) is None
    assert parse_optional_duration_ms("2h") == 7_200_000


def test_parse_interval_rejects_sub_minimum() -> None:
    with pytest.raises(ValueError, match="at least"):
        parse_interval_ms("1s")


def test_parse_interval_invalid() -> None:
    with pytest.raises(ValueError):
        parse_interval_ms("nope")


@pytest.mark.case("T1-SCHED-01")
def test_interval_parse_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MONKEYBOT_SCHEDULER_MIN_INTERVAL_MS", raising=False)
    with pytest.raises(ValueError):
        parse_interval_ms("4s")
    assert parse_interval_ms("5s") == 5000
    assert parse_interval_ms("5m") == 300000


async def _insert_malformed(
    store: SQLiteScheduledLoopStore, loop_id: str, interval_ms: object
) -> None:
    await store._conn.execute(
        """
        INSERT INTO scheduled_loops(
            loop_id, session_id, status, prompt, interval_ms,
            max_ticks, max_runtime_ms, skip_if_busy, tick_index,
            next_tick_at_ms, started_at_ms, tick_in_flight, kind,
            consecutive_error_count
        ) VALUES (?, ?, 'active', ?, ?, 1, NULL, 1, 0, 0, 0, 0, 'loop', 0)
        """,
        (loop_id, "sess", "malformed", interval_ms),
    )
    await store._conn.commit()


@pytest.mark.case("T1-SCHED-01")
@pytest.mark.asyncio
async def test_list_due_skips_malformed_stored_intervals(tmp_path: Path) -> None:
    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path / 'loops.db'}")
    await backend.open()
    store = backend.scheduled_loops()
    await _insert_malformed(store, "zero-interval", 0)
    await _insert_malformed(store, "garbage-interval", "not-an-interval")
    await store.create(
        ScheduledLoopCreate(
            prompt="good",
            interval_ms=5000,
            loop_id="good-loop",
            max_ticks=1,
        )
    )
    due = await store.list_due(10**15)
    ids = {row.loop_id for row in due}
    await backend.close()
    assert "good-loop" in ids
    assert "zero-interval" not in ids
    assert "garbage-interval" not in ids
