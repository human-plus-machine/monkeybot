from __future__ import annotations

import aiosqlite
import pytest

from monkeybot.core.durable_runs import DurableRunStore


@pytest.fixture
async def store(tmp_path: pytest.TempPathFactory) -> DurableRunStore:
    s = DurableRunStore(str(tmp_path / "test.db"))
    await s.init()
    return s


async def test_record_started_inserts_row(
    store: DurableRunStore, tmp_path: pytest.TempPathFactory
) -> None:
    """record_started creates a row with status='running' and NULL completed_at."""
    await store.record_started("run-1", "script.py", "/scratch/1")
    async with aiosqlite.connect(str(tmp_path / "test.db")) as db:
        async with db.execute(
            "SELECT status, completed_at FROM durable_runs WHERE run_id='run-1'"
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row[0] == "running"
    assert row[1] is None


async def test_record_started_idempotent(store: DurableRunStore) -> None:
    """Calling record_started twice with the same run_id results in exactly one row."""
    await store.record_started("run-1", "script.py", "/scratch/1")
    await store.record_started("run-1", "script.py", "/scratch/1")
    runs = await store.pending_runs()
    assert len([r for r in runs if r["run_id"] == "run-1"]) == 1


async def test_record_completed_transitions(
    store: DurableRunStore, tmp_path: pytest.TempPathFactory
) -> None:
    """record_completed sets status='completed' and populates completed_at."""
    await store.record_started("run-1", "script.py", "/scratch/1")
    await store.record_completed("run-1")
    async with aiosqlite.connect(str(tmp_path / "test.db")) as db:
        async with db.execute(
            "SELECT status, completed_at FROM durable_runs WHERE run_id='run-1'"
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row[0] == "completed"
    assert row[1] is not None


async def test_record_failed_transitions(
    store: DurableRunStore, tmp_path: pytest.TempPathFactory
) -> None:
    """record_failed sets status='failed', error_msg, and completed_at."""
    await store.record_started("run-1", "script.py", "/scratch/1")
    await store.record_failed("run-1", "something went wrong")
    async with aiosqlite.connect(str(tmp_path / "test.db")) as db:
        async with db.execute(
            "SELECT status, error_msg, completed_at FROM durable_runs WHERE run_id='run-1'"
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row[0] == "failed"
    assert row[1] == "something went wrong"
    assert row[2] is not None


async def test_record_completed_idempotent(
    store: DurableRunStore, tmp_path: pytest.TempPathFactory
) -> None:
    """record_failed after record_completed is a no-op — status stays 'completed'."""
    await store.record_started("run-1", "script.py", "/scratch/1")
    await store.record_completed("run-1")
    await store.record_failed("run-1", "late error")
    async with aiosqlite.connect(str(tmp_path / "test.db")) as db:
        async with db.execute(
            "SELECT status FROM durable_runs WHERE run_id='run-1'"
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row[0] == "completed"


async def test_pending_runs_returns_running(store: DurableRunStore) -> None:
    """pending_runs returns only rows with status='running'."""
    await store.record_started("run-1", "script.py", "/scratch/1")
    await store.record_started("run-2", "script.py", "/scratch/2")
    await store.record_completed("run-2")
    pending = await store.pending_runs()
    assert len(pending) == 1
    assert pending[0]["run_id"] == "run-1"


async def test_pending_runs_empty(store: DurableRunStore) -> None:
    """pending_runs returns [] when all runs are terminal."""
    await store.record_started("run-1", "script.py", "/scratch/1")
    await store.record_completed("run-1")
    assert await store.pending_runs() == []


async def test_init_creates_db_file(tmp_path: pytest.TempPathFactory) -> None:
    """init() creates the DB file even when parent directories don't exist."""
    db_path = tmp_path / "a" / "b" / "test.db"  # type: ignore[operator]
    s = DurableRunStore(str(db_path))
    await s.init()
    assert db_path.exists()


async def test_init_idempotent(tmp_path: pytest.TempPathFactory) -> None:
    """Calling init() twice does not raise and data survives."""
    s = DurableRunStore(str(tmp_path / "test.db"))
    await s.init()
    await s.record_started("run-1", "script.py", "/scratch/1")
    await s.init()
    pending = await s.pending_runs()
    assert len(pending) == 1
    assert pending[0]["run_id"] == "run-1"


async def test_wal_mode_enabled(tmp_path: pytest.TempPathFactory) -> None:
    """After init(), the DB journal_mode is 'wal'."""
    s = DurableRunStore(str(tmp_path / "test.db"))
    await s.init()
    async with aiosqlite.connect(str(tmp_path / "test.db")) as db:
        async with db.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row[0] == "wal"
