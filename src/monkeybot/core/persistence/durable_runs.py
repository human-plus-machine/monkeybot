"""Durable subprocess run tracking in SQLite."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import aiosqlite

from monkeybot.core.persistence.scheduled_loops import resolve_now_ms
from monkeybot.core.persistence.sqlite import TaskReentrantLock, with_conn_lock

# Single envelope type for stdin protocol + durable persistence (avoid drift).
from monkeybot.core.subagents.subagent_proto import SubagentEnvelope

_SUBAGENT_COLUMNS: tuple[str, ...] = (
    "run_id",
    "parent_run_id",
    "script",
    "envelope_json",
    "status",
    "result_json",
    "error_json",
    "started_at",
    "finished_at",
    "scratch_dir",
    "worker_id",
    "claimed_at",
)

__all__ = [
    "SQLiteRunStore",
    "SubagentEnvelope",
    "SubagentRunRow",
]


@dataclass(frozen=True)
class SubagentRunRow:
    """One row from the ``subagent_runs`` table."""

    run_id: str
    parent_run_id: str | None
    script: str
    envelope_json: str
    status: str
    result_json: str | None
    error_json: str | None
    started_at: int
    finished_at: int | None
    scratch_dir: str
    worker_id: str | None = None
    claimed_at: int | None = None


def _tuple_to_run_row(row: tuple[object, ...]) -> SubagentRunRow:
    d = dict(zip(_SUBAGENT_COLUMNS, row, strict=True))
    return SubagentRunRow(
        run_id=str(d["run_id"]),
        parent_run_id=str(d["parent_run_id"]) if d["parent_run_id"] is not None else None,
        script=str(d["script"]),
        envelope_json=str(d["envelope_json"]),
        status=str(d["status"]),
        result_json=str(d["result_json"]) if d["result_json"] is not None else None,
        error_json=str(d["error_json"]) if d["error_json"] is not None else None,
        started_at=int(cast(int, d["started_at"])),
        finished_at=int(cast(int, d["finished_at"])) if d["finished_at"] is not None else None,
        scratch_dir=str(d["scratch_dir"]),
        worker_id=str(d["worker_id"]) if d["worker_id"] is not None else None,
        claimed_at=int(cast(int, d["claimed_at"])) if d["claimed_at"] is not None else None,
    )


class SQLiteRunStore:
    """Persist lifecycle rows for subprocess subagents."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        lock: asyncio.Lock | TaskReentrantLock | None = None,
    ) -> None:
        self._conn = conn
        self._lock = lock or TaskReentrantLock()

    @with_conn_lock
    async def record_started(
        self,
        run_id: str,
        parent_run_id: str | None,
        script: str,
        envelope: SubagentEnvelope,
        scratch_dir: Path,
        *,
        owner_id: str | None = None,
    ) -> None:
        """Insert a ``running`` row. ``owner_id`` also starts the claim lease."""
        await self._record_run(
            "running", run_id, parent_run_id, script, envelope, scratch_dir, owner_id=owner_id
        )

    async def _record_run(
        self,
        status: str,
        run_id: str,
        parent_run_id: str | None,
        script: str,
        envelope: SubagentEnvelope,
        scratch_dir: Path,
        *,
        owner_id: str | None = None,
    ) -> None:
        now_ms = int(time.time() * 1000)
        claimed_at = now_ms if owner_id else None
        await self._conn.execute(
            """
            INSERT INTO subagent_runs(
                run_id, parent_run_id, script, envelope_json,
                status, result_json, error_json, started_at, finished_at, scratch_dir,
                worker_id, claimed_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?)
            """,
            (
                run_id,
                parent_run_id,
                script,
                envelope.to_json(),
                status,
                now_ms,
                str(scratch_dir),
                owner_id,
                claimed_at,
            ),
        )
        await self._conn.commit()

    @with_conn_lock
    async def renew_claim(self, run_id: str, worker_id: str) -> bool:
        """Extend the running claim lease for a worker still executing the run."""
        now_ms = int(time.time() * 1000)
        cursor = await self._conn.execute(
            """
            UPDATE subagent_runs
            SET claimed_at = ?
            WHERE run_id = ?
              AND status = 'running'
              AND worker_id = ?
            """,
            (now_ms, run_id, worker_id),
        )
        await self._conn.commit()
        return int(cursor.rowcount) == 1

    @with_conn_lock
    async def list_stale_claims(
        self, stale_after_ms: int, now_ms: int | None = None
    ) -> list[SubagentRunRow]:
        """Return ``running`` rows whose claim lease is older than ``stale_after_ms``."""
        cutoff = resolve_now_ms(now_ms) - stale_after_ms
        columns = ", ".join(_SUBAGENT_COLUMNS)
        cursor = await self._conn.execute(
            f"""
            SELECT {columns} FROM subagent_runs
            WHERE status = 'running'
              AND claimed_at IS NOT NULL
              AND claimed_at < ?
            """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [_tuple_to_run_row(tuple(r)) for r in rows]

    @with_conn_lock
    async def record_completed(
        self,
        run_id: str,
        result_json: str,
        *,
        worker_id: str | None = None,
    ) -> bool:
        """Mark run completed with payload.

        When ``worker_id`` is set, only succeeds if that worker still owns a
        ``running`` claim (atomic; avoids TOCTOU overwrite after reclaim).
        """
        now_ms = int(time.time() * 1000)
        if worker_id is None:
            cursor = await self._conn.execute(
                """
                UPDATE subagent_runs
                SET status = 'completed',
                    result_json = ?,
                    finished_at = ?,
                    error_json = NULL
                WHERE run_id = ?
                """,
                (result_json, now_ms, run_id),
            )
        else:
            cursor = await self._conn.execute(
                """
                UPDATE subagent_runs
                SET status = 'completed',
                    result_json = ?,
                    finished_at = ?,
                    error_json = NULL
                WHERE run_id = ?
                  AND status = 'running'
                  AND worker_id = ?
                """,
                (result_json, now_ms, run_id, worker_id),
            )
        await self._conn.commit()
        return int(cursor.rowcount) == 1

    @with_conn_lock
    async def record_failed(
        self,
        run_id: str,
        error: str,
        *,
        worker_id: str | None = None,
        result_json: str | None = None,
    ) -> bool:
        """Mark run failed. ``result_json`` keeps the task payload for a later await_task.

        When ``worker_id`` is set, only succeeds if that worker still owns a
        ``running`` claim.
        """
        now_ms = int(time.time() * 1000)
        err_payload = json.dumps({"message": error})
        if worker_id is None:
            cursor = await self._conn.execute(
                """
                UPDATE subagent_runs
                SET status = 'failed',
                    error_json = ?,
                    result_json = COALESCE(?, result_json),
                    finished_at = ?
                WHERE run_id = ?
                """,
                (err_payload, result_json, now_ms, run_id),
            )
        else:
            cursor = await self._conn.execute(
                """
                UPDATE subagent_runs
                SET status = 'failed',
                    error_json = ?,
                    result_json = COALESCE(?, result_json),
                    finished_at = ?
                WHERE run_id = ?
                  AND status = 'running'
                  AND worker_id = ?
                """,
                (err_payload, result_json, now_ms, run_id, worker_id),
            )
        await self._conn.commit()
        return int(cursor.rowcount) == 1

    @with_conn_lock
    async def list_running(self) -> list[SubagentRunRow]:
        """Return ``running`` rows oldest-first (orphan sweep)."""
        columns = ", ".join(_SUBAGENT_COLUMNS)
        cursor = await self._conn.execute(
            f"""
            SELECT {columns} FROM subagent_runs
            WHERE status = 'running'
            ORDER BY started_at ASC
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_tuple_to_run_row(tuple(row)) for row in rows]

    @with_conn_lock
    async def get_run(self, run_id: str) -> SubagentRunRow | None:
        """Return full row or ``None``."""
        columns = ", ".join(_SUBAGENT_COLUMNS)
        cursor = await self._conn.execute(
            f"SELECT {columns} FROM subagent_runs WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return _tuple_to_run_row(tuple(row))
