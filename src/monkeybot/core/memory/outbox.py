"""Durable memory outbox: row model, SQL helpers, and the store protocol."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import aiosqlite

from monkeybot.core.memory.ids import outbox_id, utc_now_iso
from monkeybot.core.persistence.sqlite import OUTBOX_DDL, OUTBOX_INDEX_DDL, ConnLock

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMMITTED = "committed"
STATUS_DEAD = "dead"

_LEASE_SECONDS = 30
_GC_AFTER_DAYS = 7

_PERMANENT_ERROR_CLASSES = frozenset(
    {
        "ValueError",
        "TypeError",
        "UnicodeError",
        "UnicodeDecodeError",
        "UnicodeEncodeError",
        "PermanentMemoryError",
    }
)

# Column order is coupled to OutboxRow and _row_from_sql.
_OUTBOX_SELECT = """
        SELECT id, agent_id, thread_id, turn_id, message_id, role, content, workspace_id,
               wing, room, created_at, status, attempts, next_attempt_at,
               last_error, traceparent, lease_owner, lease_expires_at, palace_id
        FROM memory_outbox
"""


class PermanentMemoryError(ValueError):
    """Classified as a permanent outbox failure (dead-letter immediately)."""


@dataclass(frozen=True)
class OutboxRow:
    id: str
    agent_id: str
    thread_id: str
    turn_id: str
    message_id: str
    role: str
    content: str | None
    workspace_id: str | None
    wing: str
    room: str
    created_at: str
    status: str
    attempts: int
    next_attempt_at: str | None
    last_error: str | None
    traceparent: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    palace_id: str = ""

    def metadata(self) -> dict[str, str]:
        meta = {
            "wing": self.wing,
            "room": self.room,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "message_id": self.message_id,
            "role": self.role,
            "source_timestamp": self.created_at,
            "filed_at": self.created_at,
            "added_by": "monkeybot",
            "agent_id": self.agent_id,
        }
        if self.workspace_id:
            meta["workspace_id"] = self.workspace_id
        return meta


class OutboxStore(Protocol):
    """Durable outbox used by the memory writer. Implemented per storage backend."""

    async def insert_pending(
        self,
        *,
        agent_id: str,
        thread_id: str,
        turn_id: str,
        message_id: str,
        role: str,
        content: str,
        workspace_id: str | None,
        wing: str,
        room: str,
        created_at: str | None = None,
        traceparent: str | None = None,
        palace_id: str = "",
        commit: bool = True,
    ) -> str | None: ...

    async def claim_batch(
        self,
        *,
        agent_id: str,
        lease_owner: str,
        palace_id: str = "",
        limit: int = 16,
        lease_seconds: int = _LEASE_SECONDS,
    ) -> list[OutboxRow]: ...

    async def mark_committed(
        self, row_ids: list[str], *, lease_owner: str | None = None
    ) -> int: ...

    async def mark_retry(
        self,
        row_id: str,
        *,
        error_class: str,
        attempts: int,
        permanent: bool | None = None,
        lease_owner: str | None = None,
    ) -> int: ...

    async def gc_committed(self, *, days: int = _GC_AFTER_DAYS) -> int: ...

    async def pending_depth(self, *, agent_id: str | None = None) -> tuple[int, float]: ...

    async def dead_depth(self, *, agent_id: str | None = None) -> int: ...

    async def close(self) -> None: ...


def is_permanent_error(error_class: str) -> bool:
    return error_class in _PERMANENT_ERROR_CLASSES


def _row_from_sql(row: Any) -> OutboxRow:
    return OutboxRow(
        id=str(row[0]),
        agent_id=str(row[1] or ""),
        thread_id=str(row[2]),
        turn_id=str(row[3]),
        message_id=str(row[4]),
        role=str(row[5]),
        content=row[6],
        workspace_id=row[7],
        wing=str(row[8]),
        room=str(row[9]),
        created_at=str(row[10]),
        status=str(row[11]),
        attempts=int(row[12] or 0),
        next_attempt_at=row[13],
        last_error=row[14],
        traceparent=row[15],
        lease_owner=row[16],
        lease_expires_at=row[17],
        palace_id=str(row[18] or ""),
    )


async def ensure_outbox_schema(conn: aiosqlite.Connection) -> None:
    from monkeybot.core.persistence import sqlite as sqlite_mod

    await conn.execute(OUTBOX_DDL)
    await conn.execute(OUTBOX_INDEX_DDL)
    await sqlite_mod._ensure_outbox_agent_id_column(conn)
    await sqlite_mod._ensure_outbox_palace_id_column(conn)


def backoff_iso(attempts: int, *, now: datetime | None = None) -> str:
    base = now or datetime.now(UTC)
    delay = min(300, 2 ** max(0, attempts))
    return (base + timedelta(seconds=delay)).isoformat(timespec="seconds")


async def insert_pending(
    conn: aiosqlite.Connection,
    *,
    agent_id: str,
    thread_id: str,
    turn_id: str,
    message_id: str,
    role: str,
    content: str,
    workspace_id: str | None,
    wing: str,
    room: str,
    created_at: str | None = None,
    traceparent: str | None = None,
    palace_id: str = "",
    commit: bool = True,
) -> str | None:
    """Insert a pending outbox row. Returns None when the id is already committed."""
    row_id = outbox_id(agent_id=agent_id, thread_id=thread_id, message_id=message_id, role=role)
    cur = await conn.execute("SELECT status FROM memory_outbox WHERE id = ?", (row_id,))
    existing = await cur.fetchone()
    await cur.close()
    if existing is not None:
        status = str(existing[0])
        if status == STATUS_COMMITTED:
            return None
        return row_id
    await conn.execute(
        """
        INSERT INTO memory_outbox (
          id, agent_id, thread_id, turn_id, message_id, role, content, workspace_id,
          wing, room, created_at, status, attempts, traceparent, palace_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
        """,
        (
            row_id,
            agent_id,
            thread_id,
            turn_id,
            message_id,
            role,
            content,
            workspace_id,
            wing,
            room,
            created_at or utc_now_iso(),
            traceparent,
            palace_id,
        ),
    )
    if commit:
        await conn.commit()
    return row_id


async def claim_batch(
    conn: aiosqlite.Connection,
    *,
    lease_owner: str,
    limit: int = 16,
    lease_seconds: int = _LEASE_SECONDS,
    agent_id: str | None = None,
    palace_id: str = "",
) -> list[OutboxRow]:
    now = datetime.now(UTC)
    now_iso = now.isoformat(timespec="seconds")
    expires = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
    await conn.execute("BEGIN IMMEDIATE")
    expire_sql = """
        UPDATE memory_outbox
        SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL
        WHERE status = 'processing' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
    """
    expire_params: list[Any] = [now_iso]
    if agent_id:
        expire_sql += " AND agent_id = ?"
        expire_params.append(agent_id)
    await conn.execute(expire_sql, tuple(expire_params))
    params: list[Any] = []
    where = [
        "status = 'pending'",
        "(next_attempt_at IS NULL OR next_attempt_at <= ?)",
    ]
    params.append(now_iso)
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    if palace_id:
        where.append("(palace_id = ? OR palace_id = '' OR palace_id IS NULL)")
        params.append(palace_id)
    params.append(limit)
    cur = await conn.execute(
        _OUTBOX_SELECT
        + f"""
        WHERE {" AND ".join(where)}
        ORDER BY created_at ASC
        LIMIT ?
        """,
        tuple(params),
    )
    rows = await cur.fetchall()
    await cur.close()
    claimed: list[OutboxRow] = []
    for raw in rows:
        row_id = str(raw[0])
        await conn.execute(
            """
            UPDATE memory_outbox
            SET status = 'processing', lease_owner = ?, lease_expires_at = ?,
                attempts = attempts + 1, palace_id = CASE
                    WHEN palace_id IS NULL OR palace_id = '' THEN ?
                    ELSE palace_id
                END
            WHERE id = ?
            """,
            (lease_owner, expires, palace_id, row_id),
        )
        claimed.append(_row_from_sql(raw))
    await conn.commit()
    return claimed


async def mark_committed(
    conn: aiosqlite.Connection,
    row_ids: list[str],
    *,
    lease_owner: str | None = None,
) -> int:
    if not row_ids:
        return 0
    placeholders = ",".join("?" * len(row_ids))
    sql = f"""
        UPDATE memory_outbox
        SET status = 'committed', lease_owner = NULL, lease_expires_at = NULL,
            last_error = NULL, next_attempt_at = NULL
        WHERE id IN ({placeholders})
    """
    params: list[Any] = list(row_ids)
    if lease_owner:
        sql += " AND lease_owner = ?"
        params.append(lease_owner)
    cur = await conn.execute(sql, tuple(params))
    await conn.commit()
    return int(cur.rowcount or 0)


async def mark_retry(
    conn: aiosqlite.Connection,
    row_id: str,
    *,
    error_class: str,
    attempts: int,
    permanent: bool | None = None,
    lease_owner: str | None = None,
) -> int:
    dead = bool(permanent) if permanent is not None else is_permanent_error(error_class)
    status = STATUS_DEAD if dead else STATUS_PENDING
    next_at = None if status == STATUS_DEAD else backoff_iso(attempts)
    sql = """
        UPDATE memory_outbox
        SET status = ?, last_error = ?, next_attempt_at = ?,
            lease_owner = NULL, lease_expires_at = NULL
        WHERE id = ?
    """
    params: list[Any] = [status, error_class, next_at, row_id]
    if lease_owner:
        sql += " AND lease_owner = ?"
        params.append(lease_owner)
    cur = await conn.execute(sql, tuple(params))
    await conn.commit()
    return int(cur.rowcount or 0)


async def gc_committed(conn: aiosqlite.Connection, *, days: int = _GC_AFTER_DAYS) -> int:
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    cur = await conn.execute(
        """
        DELETE FROM memory_outbox
        WHERE status = 'committed' AND created_at < ?
        """,
        (cutoff,),
    )
    await conn.commit()
    return int(cur.rowcount or 0)


def _age_from_oldest(oldest: Any) -> float:
    if not oldest:
        return 0.0
    try:
        created = datetime.fromisoformat(str(oldest))
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return max(0.0, (datetime.now(UTC) - created).total_seconds())
    except ValueError:
        return 0.0


async def pending_depth(
    conn: aiosqlite.Connection, *, agent_id: str | None = None
) -> tuple[int, float]:
    if agent_id:
        cur = await conn.execute(
            """
            SELECT COUNT(*), MIN(created_at)
            FROM memory_outbox
            WHERE status IN ('pending', 'processing') AND agent_id = ?
            """,
            (agent_id,),
        )
    else:
        cur = await conn.execute(
            """
            SELECT COUNT(*), MIN(created_at)
            FROM memory_outbox
            WHERE status IN ('pending', 'processing')
            """
        )
    row = await cur.fetchone()
    await cur.close()
    count = int(row[0] or 0) if row else 0
    oldest = row[1] if row else None
    return count, _age_from_oldest(oldest)


async def dead_depth(conn: aiosqlite.Connection, *, agent_id: str | None = None) -> int:
    if agent_id:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM memory_outbox WHERE status = 'dead' AND agent_id = ?",
            (agent_id,),
        )
    else:
        cur = await conn.execute("SELECT COUNT(*) FROM memory_outbox WHERE status = 'dead'")
    row = await cur.fetchone()
    await cur.close()
    return int(row[0] or 0) if row else 0


class SqliteOutboxStore:
    """OutboxStore backed by a shared aiosqlite connection."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        owns_connection: bool = False,
        lock: ConnLock | None = None,
    ) -> None:
        self._conn = conn
        self._owns_connection = owns_connection
        self._lock = lock or asyncio.Lock()

    async def insert_pending(self, **kwargs: Any) -> str | None:
        commit = bool(kwargs.get("commit", True))
        if not commit:
            return await insert_pending(self._conn, **kwargs)
        async with self._lock:
            return await insert_pending(self._conn, **kwargs)

    async def claim_batch(
        self,
        *,
        agent_id: str,
        lease_owner: str,
        limit: int = 16,
        lease_seconds: int = _LEASE_SECONDS,
        palace_id: str = "",
    ) -> list[OutboxRow]:
        async with self._lock:
            return await claim_batch(
                self._conn,
                agent_id=agent_id,
                lease_owner=lease_owner,
                limit=limit,
                lease_seconds=lease_seconds,
                palace_id=palace_id,
            )

    async def mark_committed(self, row_ids: list[str], *, lease_owner: str | None = None) -> int:
        async with self._lock:
            return await mark_committed(self._conn, row_ids, lease_owner=lease_owner)

    async def mark_retry(
        self,
        row_id: str,
        *,
        error_class: str,
        attempts: int,
        permanent: bool | None = None,
        lease_owner: str | None = None,
    ) -> int:
        async with self._lock:
            return await mark_retry(
                self._conn,
                row_id,
                error_class=error_class,
                attempts=attempts,
                permanent=permanent,
                lease_owner=lease_owner,
            )

    async def gc_committed(self, *, days: int = _GC_AFTER_DAYS) -> int:
        async with self._lock:
            return await gc_committed(self._conn, days=days)

    async def pending_depth(self, *, agent_id: str | None = None) -> tuple[int, float]:
        async with self._lock:
            return await pending_depth(self._conn, agent_id=agent_id)

    async def dead_depth(self, *, agent_id: str | None = None) -> int:
        async with self._lock:
            return await dead_depth(self._conn, agent_id=agent_id)

    async def close(self) -> None:
        if self._owns_connection:
            await self._conn.close()
