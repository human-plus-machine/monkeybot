"""Durable memory outbox in the agent SQLite database."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from monkeybot.core.memory.ids import outbox_id, utc_now_iso
from monkeybot.core.persistence.sqlite import OUTBOX_DDL, OUTBOX_INDEX_DDL

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMMITTED = "committed"
STATUS_DEAD = "dead"

_MAX_ATTEMPTS = 8
_LEASE_SECONDS = 30
_GC_AFTER_DAYS = 7


@dataclass(frozen=True)
class OutboxRow:
    id: str
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
        }
        if self.workspace_id:
            meta["workspace_id"] = self.workspace_id
        return meta


def _row_from_sql(row: Any) -> OutboxRow:
    return OutboxRow(
        id=str(row[0]),
        thread_id=str(row[1]),
        turn_id=str(row[2]),
        message_id=str(row[3]),
        role=str(row[4]),
        content=row[5],
        workspace_id=row[6],
        wing=str(row[7]),
        room=str(row[8]),
        created_at=str(row[9]),
        status=str(row[10]),
        attempts=int(row[11] or 0),
        next_attempt_at=row[12],
        last_error=row[13],
        traceparent=row[14],
        lease_owner=row[15],
        lease_expires_at=row[16],
    )


async def ensure_outbox_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute(OUTBOX_DDL)
    await conn.execute(OUTBOX_INDEX_DDL)
    await conn.commit()


def backoff_iso(attempts: int, *, now: datetime | None = None) -> str:
    base = now or datetime.now(timezone.utc)
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
    commit: bool = True,
) -> str | None:
    """Insert a pending outbox row. Returns None when the id is already committed."""
    row_id = outbox_id(
        agent_id=agent_id, thread_id=thread_id, message_id=message_id, role=role
    )
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
          id, thread_id, turn_id, message_id, role, content, workspace_id,
          wing, room, created_at, status, attempts, traceparent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)
        """,
        (
            row_id,
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
) -> list[OutboxRow]:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    expires = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
    await conn.execute("BEGIN IMMEDIATE")
    await conn.execute(
        """
        UPDATE memory_outbox
        SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL
        WHERE status = 'processing' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
        """,
        (now_iso,),
    )
    cur = await conn.execute(
        """
        SELECT id, thread_id, turn_id, message_id, role, content, workspace_id,
               wing, room, created_at, status, attempts, next_attempt_at,
               last_error, traceparent, lease_owner, lease_expires_at
        FROM memory_outbox
        WHERE status = 'pending'
          AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (now_iso, limit),
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
                attempts = attempts + 1
            WHERE id = ?
            """,
            (lease_owner, expires, row_id),
        )
        claimed.append(_row_from_sql(raw))
    await conn.commit()
    return claimed


async def mark_committed(conn: aiosqlite.Connection, row_ids: list[str]) -> None:
    if not row_ids:
        return
    placeholders = ",".join("?" * len(row_ids))
    await conn.execute(
        f"""
        UPDATE memory_outbox
        SET status = 'committed', lease_owner = NULL, lease_expires_at = NULL,
            last_error = NULL, next_attempt_at = NULL
        WHERE id IN ({placeholders})
        """,
        tuple(row_ids),
    )
    await conn.commit()


async def mark_retry(
    conn: aiosqlite.Connection,
    row_id: str,
    *,
    error_class: str,
    attempts: int,
) -> None:
    status = STATUS_DEAD if attempts >= _MAX_ATTEMPTS else STATUS_PENDING
    next_at = None if status == STATUS_DEAD else backoff_iso(attempts)
    await conn.execute(
        """
        UPDATE memory_outbox
        SET status = ?, last_error = ?, next_attempt_at = ?,
            lease_owner = NULL, lease_expires_at = NULL
        WHERE id = ?
        """,
        (status, error_class, next_at, row_id),
    )
    await conn.commit()


async def gc_committed(conn: aiosqlite.Connection, *, days: int = _GC_AFTER_DAYS) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    cur = await conn.execute(
        """
        UPDATE memory_outbox
        SET content = NULL
        WHERE status = 'committed' AND content IS NOT NULL AND created_at < ?
        """,
        (cutoff,),
    )
    await conn.commit()
    return int(cur.rowcount or 0)


async def pending_depth(conn: aiosqlite.Connection) -> tuple[int, float]:
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
    age = 0.0
    if oldest:
        try:
            created = datetime.fromisoformat(str(oldest))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - created).total_seconds())
        except ValueError:
            age = 0.0
    return count, age
