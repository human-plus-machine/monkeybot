"""Session branch lineage. Each branch is its own linear history thread.

The root branch's thread id is the session id. Child branches use
``branch:{session_id}:{branch_id}`` and are hidden from ``list_threads``.
Compaction and the verifier only ever see one linear thread.
"""

from __future__ import annotations

import builtins
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

import aiosqlite

from monkeybot.core.persistence.sqlite import TaskReentrantLock, with_conn_lock
from monkeybot.core.persistence.thread_summary import BRANCH_THREAD_ID_PREFIX

ROOT_BRANCH_ID = "root"

# ``fork_fingerprint`` of a branch that diverges before the first row (an edit
# or regenerate of the first message). Its ``fork_row_index`` is -1.
START_FORK_FINGERPRINT = "start"

# Stays under SQLite's bound-parameter limit.
_IN_CHUNK = 500

_COLUMNS = (
    "branch_id",
    "session_id",
    "thread_id",
    "parent_branch_id",
    "fork_row_index",
    "fork_fingerprint",
    "op",
    "created_at",
    "last_active_at",
    "is_active",
    "inherited_forks",
)


def encode_fork_keys(keys: Sequence[str]) -> str | None:
    return json.dumps(list(keys)) if keys else None


def decode_fork_keys(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, list):
        values: Any = raw
    elif isinstance(raw, str) and raw:
        values = json.loads(raw)
    else:
        return ()
    return tuple(str(value) for value in values if isinstance(value, str))


def branch_thread_id(session_id: str, branch_id: str) -> str:
    """History thread id for a non-root branch. Hidden from ``list_threads``."""
    return f"{BRANCH_THREAD_ID_PREFIX}{session_id}:{branch_id}"


@dataclass(frozen=True)
class BranchRecord:
    """One branch of a session's conversation tree."""

    branch_id: str
    session_id: str
    thread_id: str
    parent_branch_id: str | None
    fork_row_index: int | None
    fork_fingerprint: str | None
    op: str | None
    created_at: int
    last_active_at: int
    is_active: bool
    # Branches whose fork row this branch's copied prefix kept, so their
    # navigator shows here. Fixed at creation; later compaction cannot change it.
    inherited_forks: tuple[str, ...] = ()


def _record_from_row(row: Any) -> BranchRecord:
    parent = row[3]
    fork_index = row[4]
    fingerprint = row[5]
    op = row[6]
    return BranchRecord(
        branch_id=str(row[0]),
        session_id=str(row[1]),
        thread_id=str(row[2]),
        parent_branch_id=str(parent) if parent is not None else None,
        fork_row_index=int(fork_index) if fork_index is not None else None,
        fork_fingerprint=str(fingerprint) if fingerprint is not None else None,
        op=str(op) if op is not None else None,
        created_at=int(row[7]),
        last_active_at=int(row[8]),
        is_active=bool(row[9]),
        inherited_forks=decode_fork_keys(row[10]),
    )


@runtime_checkable
class BranchStore(Protocol):
    """Durable branch lineage for one storage backend."""

    async def ensure_root(self, session_id: str) -> BranchRecord: ...

    async def create(
        self,
        record: BranchRecord,
        *,
        make_active: bool,
    ) -> BranchRecord: ...

    async def list(self, session_id: str) -> list[BranchRecord]: ...

    async def get(self, session_id: str, branch_id: str) -> BranchRecord | None: ...

    async def get_active(self, session_id: str) -> BranchRecord | None: ...

    async def set_active(self, session_id: str, branch_id: str) -> BranchRecord | None: ...

    async def touch(self, session_id: str, branch_id: str) -> None: ...

    async def active_non_root(self, session_ids: Sequence[str]) -> dict[str, BranchRecord]:
        """Active branch per session, for sessions whose active branch is not the root."""
        ...

    # `list` on this class shadows the builtin in later annotations.
    async def delete_session(self, session_id: str) -> builtins.list[BranchRecord]: ...


class SQLiteBranchStore:
    """SQLite-backed branch lineage."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        lock: TaskReentrantLock | None = None,
    ) -> None:
        self._conn = conn
        self._lock = lock or TaskReentrantLock()

    async def _select_one(self, session_id: str, branch_id: str) -> BranchRecord | None:
        columns = ", ".join(_COLUMNS)
        cursor = await self._conn.execute(
            f"SELECT {columns} FROM session_branches WHERE session_id = ? AND branch_id = ?",
            (session_id, branch_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return _record_from_row(row)

    async def _select_active(self, session_id: str) -> BranchRecord | None:
        columns = ", ".join(_COLUMNS)
        cursor = await self._conn.execute(
            f"""
            SELECT {columns} FROM session_branches
            WHERE session_id = ? AND is_active = 1
            LIMIT 1
            """,
            (session_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return _record_from_row(row)

    async def _insert(self, record: BranchRecord) -> None:
        await self._conn.execute(
            """
            INSERT INTO session_branches(
                branch_id, session_id, thread_id, parent_branch_id,
                fork_row_index, fork_fingerprint, op, created_at, last_active_at, is_active,
                inherited_forks
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.branch_id,
                record.session_id,
                record.thread_id,
                record.parent_branch_id,
                record.fork_row_index,
                record.fork_fingerprint,
                record.op,
                record.created_at,
                record.last_active_at,
                1 if record.is_active else 0,
                encode_fork_keys(record.inherited_forks),
            ),
        )

    async def _deactivate(self, session_id: str) -> None:
        await self._conn.execute(
            "UPDATE session_branches SET is_active = 0 WHERE session_id = ?",
            (session_id,),
        )

    async def _ensure_root(self, session_id: str) -> BranchRecord:
        existing = await self._select_one(session_id, ROOT_BRANCH_ID)
        if existing is not None:
            return existing
        active = await self._select_active(session_id)
        now = int(time.time() * 1000)
        record = BranchRecord(
            branch_id=ROOT_BRANCH_ID,
            session_id=session_id,
            thread_id=session_id,
            parent_branch_id=None,
            fork_row_index=None,
            fork_fingerprint=None,
            op=None,
            created_at=now,
            last_active_at=now,
            is_active=active is None,
        )
        await self._insert(record)
        return record

    @with_conn_lock
    async def ensure_root(self, session_id: str) -> BranchRecord:
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            record = await self._ensure_root(session_id)
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        return record

    @with_conn_lock
    async def create(self, record: BranchRecord, *, make_active: bool) -> BranchRecord:
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            await self._ensure_root(record.session_id)
            stored = record
            if make_active:
                await self._deactivate(record.session_id)
                stored = replace(record, is_active=True)
            await self._insert(stored)
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        return stored

    @with_conn_lock
    async def list(self, session_id: str) -> list[BranchRecord]:
        columns = ", ".join(_COLUMNS)
        cursor = await self._conn.execute(
            f"""
            SELECT {columns} FROM session_branches
            WHERE session_id = ?
            ORDER BY created_at ASC, branch_id ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_record_from_row(row) for row in rows]

    @with_conn_lock
    async def get(self, session_id: str, branch_id: str) -> BranchRecord | None:
        return await self._select_one(session_id, branch_id)

    @with_conn_lock
    async def get_active(self, session_id: str) -> BranchRecord | None:
        return await self._select_active(session_id)

    @with_conn_lock
    async def set_active(self, session_id: str, branch_id: str) -> BranchRecord | None:
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = await self._select_one(session_id, branch_id)
            if current is None:
                await self._conn.rollback()
                return None
            now = int(time.time() * 1000)
            await self._deactivate(session_id)
            await self._conn.execute(
                """
                UPDATE session_branches
                SET is_active = 1, last_active_at = ?
                WHERE session_id = ? AND branch_id = ?
                """,
                (now, session_id, branch_id),
            )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        updated = await self._select_one(session_id, branch_id)
        return updated

    @with_conn_lock
    async def touch(self, session_id: str, branch_id: str) -> None:
        now = int(time.time() * 1000)
        await self._conn.execute(
            """
            UPDATE session_branches
            SET last_active_at = ?
            WHERE session_id = ? AND branch_id = ?
            """,
            (now, session_id, branch_id),
        )
        await self._conn.commit()

    @with_conn_lock
    async def active_non_root(self, session_ids: Sequence[str]) -> dict[str, BranchRecord]:
        columns = ", ".join(_COLUMNS)
        ids = builtins.list(dict.fromkeys(session_ids))
        out: dict[str, BranchRecord] = {}
        for start in range(0, len(ids), _IN_CHUNK):
            chunk = ids[start : start + _IN_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            cursor = await self._conn.execute(
                f"""
                SELECT {columns} FROM session_branches
                WHERE is_active = 1 AND branch_id != ? AND session_id IN ({placeholders})
                """,
                (ROOT_BRANCH_ID, *chunk),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                record = _record_from_row(row)
                out[record.session_id] = record
        return out

    @with_conn_lock
    async def delete_session(self, session_id: str) -> builtins.list[BranchRecord]:
        # See BranchStore.delete_session: `list` shadows the builtin here.
        columns = ", ".join(_COLUMNS)
        cursor = await self._conn.execute(
            f"SELECT {columns} FROM session_branches WHERE session_id = ?",
            (session_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        await self._conn.execute(
            "DELETE FROM session_branches WHERE session_id = ?",
            (session_id,),
        )
        await self._conn.commit()
        return [_record_from_row(row) for row in rows]
