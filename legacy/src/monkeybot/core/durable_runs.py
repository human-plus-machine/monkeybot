from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import aiosqlite

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS durable_runs (
    run_id          TEXT    PRIMARY KEY,
    parent_run_id   TEXT,
    agent_name      TEXT,
    script          TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'running',
    scratch_dir     TEXT    NOT NULL,
    error_msg       TEXT,
    started_at      INTEGER NOT NULL,
    completed_at    INTEGER
)"""

_CREATE_IDX_STATUS = """
CREATE INDEX IF NOT EXISTS idx_durable_runs_status ON durable_runs(status)
"""

_CREATE_IDX_PARENT = """
CREATE INDEX IF NOT EXISTS idx_durable_runs_parent ON durable_runs(parent_run_id)
"""


class DurableRunStore:
    """Async SQLite-backed store for tracking subagent run lifecycle."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def init(self) -> None:
        """Create the durable_runs table and indexes. WAL + NORMAL. Safe to call multiple times."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute(_CREATE_TABLE)
            await db.execute(_CREATE_IDX_STATUS)
            await db.execute(_CREATE_IDX_PARENT)
            await db.commit()

    async def record_started(
        self,
        run_id: str,
        script: str,
        scratch_dir: str,
        parent_run_id: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        """Insert a row with status='running'. INSERT OR IGNORE — safe to call twice."""
        started_at = int(time.time() * 1000)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO durable_runs "
                "(run_id, parent_run_id, agent_name, script, status, scratch_dir, started_at) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?)",
                (run_id, parent_run_id, agent_name, script, scratch_dir, started_at),
            )
            await db.commit()

    async def record_completed(self, run_id: str) -> None:
        """Set status='completed', completed_at=now. Idempotent (no-op if already terminal)."""
        completed_at = int(time.time() * 1000)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE durable_runs SET status='completed', completed_at=? "
                "WHERE run_id=? AND status='running'",
                (completed_at, run_id),
            )
            await db.commit()

    async def record_failed(self, run_id: str, error_msg: str) -> None:
        """Set status='failed', error_msg, completed_at=now. Idempotent."""
        completed_at = int(time.time() * 1000)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE durable_runs SET status='failed', error_msg=?, completed_at=? "
                "WHERE run_id=? AND status='running'",
                (error_msg, completed_at, run_id),
            )
            await db.commit()

    async def pending_runs(self) -> list[dict[str, Any]]:
        """Return all rows where status='running'.

        Each dict has keys: run_id, agent_name, script, scratch_dir, parent_run_id, started_at.
        Returns [] if none.
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT run_id, agent_name, script, scratch_dir, parent_run_id, started_at "
                "FROM durable_runs WHERE status='running'"
            ) as cur:
                rows = await cur.fetchall()
        return [dict(row) for row in rows]
