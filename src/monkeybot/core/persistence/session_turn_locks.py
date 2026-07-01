"""SQLite durable per-session turn locks for multi-replica gateway deployments."""

from __future__ import annotations

import time

import aiosqlite


class SQLiteSessionTurnLockStore:
    """SQLite-backed exclusive turn lock per session."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def try_acquire(self, session_id: str, request_id: str) -> bool:
        now_ms = int(time.time() * 1000)
        cursor = await self._conn.execute(
            """
            UPDATE session_turn_locks
            SET request_id = ?, claimed_at_ms = ?
            WHERE session_id = ? AND request_id IS NULL
            """,
            (request_id, now_ms, session_id),
        )
        if cursor.rowcount == 1:
            await self._conn.commit()
            return True
        try:
            await self._conn.execute(
                """
                INSERT INTO session_turn_locks (session_id, request_id, claimed_at_ms)
                VALUES (?, ?, ?)
                """,
                (session_id, request_id, now_ms),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            await self._conn.rollback()
            return False

    async def release(self, session_id: str, request_id: str) -> None:
        await self._conn.execute(
            """
            UPDATE session_turn_locks
            SET request_id = NULL, claimed_at_ms = NULL
            WHERE session_id = ? AND request_id = ?
            """,
            (session_id, request_id),
        )
        await self._conn.commit()

    async def is_busy(self, session_id: str) -> bool:
        cursor = await self._conn.execute(
            """
            SELECT 1 FROM session_turn_locks
            WHERE session_id = ? AND request_id IS NOT NULL
            LIMIT 1
            """,
            (session_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None
