from __future__ import annotations

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from monkeybot.core.scheduler import JobConfig, Scheduler


@pytest.fixture
def db_path(tmp_path: pytest.TempPathFactory) -> str:
    return str(tmp_path / "sched.db")


def make_job(
    name: str = "test-job",
    cron: str = "0 * * * *",
    callable_str: str = "os.path:exists",
) -> JobConfig:
    return JobConfig(name=name, cron=cron, callable=callable_str, enabled=True)


async def test_tick_fires_overdue_job(db_path: str) -> None:
    scheduler = Scheduler(db_path, [make_job()], poll_interval=1)
    await scheduler._init_db()
    mock_fn = AsyncMock()
    scheduler._callables["test-job"] = mock_fn

    await scheduler._tick()

    mock_fn.assert_called_once()


async def test_tick_skips_future_job(db_path: str) -> None:
    scheduler = Scheduler(db_path, [make_job()], poll_interval=1)
    await scheduler._init_db()

    far_future = int(time.time() * 1000) + 10_000_000
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE job_runs SET next_run=? WHERE job_name=?",
            (far_future, "test-job"),
        )
        await db.commit()

    mock_fn = AsyncMock()
    scheduler._callables["test-job"] = mock_fn

    await scheduler._tick()

    mock_fn.assert_not_called()


async def test_tick_updates_next_run_after_fire(db_path: str) -> None:
    scheduler = Scheduler(db_path, [make_job()], poll_interval=1)
    await scheduler._init_db()

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT next_run FROM job_runs WHERE job_name=?", ("test-job",)
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    old_next_run = row[0]

    scheduler._callables["test-job"] = AsyncMock()
    await scheduler._tick()

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT last_run, next_run FROM job_runs WHERE job_name=?", ("test-job",)
        ) as cur:
            row = await cur.fetchone()

    assert row is not None
    assert row[0] is not None  # last_run set
    assert row[1] > old_next_run  # next_run advanced


async def test_tick_job_failure_continues_and_advances(db_path: str) -> None:
    scheduler = Scheduler(db_path, [make_job()], poll_interval=1)
    await scheduler._init_db()

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT next_run FROM job_runs WHERE job_name=?", ("test-job",)
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    old_next_run = row[0]

    scheduler._callables["test-job"] = AsyncMock(side_effect=RuntimeError("boom"))

    # Must not raise
    await scheduler._tick()

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT next_run FROM job_runs WHERE job_name=?", ("test-job",)
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row[0] > old_next_run


async def test_croniter_fallback(db_path: str) -> None:
    import monkeybot.core.scheduler as sched_module

    scheduler = Scheduler(db_path, [make_job()], poll_interval=1)

    original_warned = sched_module._croniter_warned
    sched_module._croniter_warned = False
    try:
        now = datetime.now(tz=UTC)
        with patch.dict("sys.modules", {"croniter": None}):
            result = scheduler._next_run("0 9 * * *", now)
        delta = result - now
        # Should be approximately +1 hour
        assert 3590 <= delta.total_seconds() <= 3610
    finally:
        sched_module._croniter_warned = original_warned


async def test_scheduler_start_stop(db_path: str) -> None:
    scheduler = Scheduler(db_path, [], poll_interval=60)

    await scheduler.start()
    assert scheduler._task is not None

    await scheduler.stop()
    assert scheduler._task is None


async def test_null_next_run_fires_immediately(db_path: str) -> None:
    """Jobs seeded with next_run=now should fire on the very first tick."""
    scheduler = Scheduler(db_path, [make_job()], poll_interval=1)
    await scheduler._init_db()

    mock_fn = AsyncMock()
    scheduler._callables["test-job"] = mock_fn

    await scheduler._tick()

    mock_fn.assert_called_once()
