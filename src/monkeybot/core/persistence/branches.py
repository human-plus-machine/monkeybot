"""Conversation branches. Each branch is its own linear history thread.

The root branch's thread is the session id itself. Other branches store a
copied prefix under ``branch:{session_id}:{branch_id}``, which ``list_threads``
hides. Copied rows keep their ``row_id``, so a branch locates where it left its
parent by id, whatever compaction has done to either thread since.
"""

from __future__ import annotations

import builtins
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

import aiosqlite

from monkeybot.core.persistence.sqlite import ConnLock, TaskReentrantLock, with_conn_lock
from monkeybot.core.persistence.thread_summary import BRANCH_THREAD_ID_PREFIX

ROOT_BRANCH_ID = "root"

# Stays under SQLite's bound-parameter limit.
_IN_CHUNK = 500

BRANCH_COLUMNS: tuple[str, ...] = (
    "branch_id",
    "session_id",
    "thread_id",
    "parent_branch_id",
    "fork_row_id",
    "op",
    "created_at",
    "last_active_at",
    "is_active",
)


def branch_thread_id(session_id: str, branch_id: str) -> str:
    """History thread id for a non-root branch."""
    return f"{BRANCH_THREAD_ID_PREFIX}{session_id}:{branch_id}"


@dataclass(frozen=True)
class BranchRecord:
    """One branch of a session's conversation tree."""

    branch_id: str
    session_id: str
    thread_id: str
    parent_branch_id: str | None
    fork_row_id: str | None
    """``row_id`` of the last row this branch shares with its parent; ``None``
    when it diverges before the first row (and for the root)."""
    op: str | None
    created_at: int
    last_active_at: int
    is_active: bool


def root_record(session_id: str, *, is_active: bool, now: int | None = None) -> BranchRecord:
    stamp = int(time.time() * 1000) if now is None else now
    return BranchRecord(
        branch_id=ROOT_BRANCH_ID,
        session_id=session_id,
        thread_id=session_id,
        parent_branch_id=None,
        fork_row_id=None,
        op=None,
        created_at=stamp,
        last_active_at=stamp,
        is_active=is_active,
    )


def implicit_root(first: BranchRecord) -> BranchRecord:
    """Root row written alongside a session's first branch.

    The root predates every branch, so it sorts first, and it was the active
    branch until ``first`` was created.
    """
    return replace(
        root_record(first.session_id, is_active=False, now=0),
        last_active_at=first.created_at,
    )


def record_from_mapping(row: Any) -> BranchRecord:
    """Build a record from a row keyed by :data:`BRANCH_COLUMNS` names."""
    parent = row["parent_branch_id"]
    fork_row_id = row["fork_row_id"]
    op = row["op"]
    return BranchRecord(
        branch_id=str(row["branch_id"]),
        session_id=str(row["session_id"]),
        thread_id=str(row["thread_id"]),
        parent_branch_id=str(parent) if parent is not None else None,
        fork_row_id=str(fork_row_id) if fork_row_id is not None else None,
        op=str(op) if op is not None else None,
        created_at=int(row["created_at"] or 0),
        last_active_at=int(row["last_active_at"] or 0),
        is_active=bool(row["is_active"]),
    )


@runtime_checkable
class BranchStore(Protocol):
    """Durable branch lineage for one storage backend and agent scope."""

    async def list(self, session_id: str) -> builtins.list[BranchRecord]:
        """Every branch of ``session_id``, oldest first. Empty until the first branch."""
        ...

    async def get_active(self, session_id: str) -> BranchRecord | None: ...

    async def create(self, record: BranchRecord) -> BranchRecord:
        """Store ``record`` as the session's active branch, creating the root first."""
        ...

    async def set_active(self, session_id: str, branch_id: str) -> BranchRecord | None:
        """Make ``branch_id`` active. ``None`` if the session has no such branch."""
        ...

    async def touch(self, session_id: str, branch_id: str) -> None:
        """Bump ``last_active_at`` so the sidebar sorts the session by branch activity."""
        ...

    async def active_non_root(self, session_ids: Sequence[str]) -> dict[str, BranchRecord]:
        """Active branch per session, for sessions whose active branch is not the root."""
        ...

    async def delete_session(self, session_id: str) -> builtins.list[BranchRecord]:
        """Delete the session's branch rows and return them."""
        ...


class SQLiteBranchStore:
    """SQLite-backed branch lineage."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        agent_scope: str = "",
        lock: ConnLock | None = None,
    ) -> None:
        self._conn = conn
        self._agent_scope = agent_scope
        self._lock = lock or TaskReentrantLock()

    async def _select(self, where: str, params: tuple[Any, ...]) -> builtins.list[BranchRecord]:
        cursor = await self._conn.execute(
            f"SELECT {', '.join(BRANCH_COLUMNS)} FROM session_branches "
            f"WHERE agent_scope = ? AND {where} ORDER BY created_at ASC, branch_id ASC",
            (self._agent_scope, *params),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [record_from_mapping(dict(zip(BRANCH_COLUMNS, row, strict=True))) for row in rows]

    async def _insert(self, record: BranchRecord) -> None:
        await self._conn.execute(
            f"INSERT INTO session_branches(agent_scope, {', '.join(BRANCH_COLUMNS)}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._agent_scope,
                record.branch_id,
                record.session_id,
                record.thread_id,
                record.parent_branch_id,
                record.fork_row_id,
                record.op,
                record.created_at,
                record.last_active_at,
                1 if record.is_active else 0,
            ),
        )

    async def _deactivate_all(self, session_id: str) -> None:
        await self._conn.execute(
            "UPDATE session_branches SET is_active = 0 WHERE agent_scope = ? AND session_id = ?",
            (self._agent_scope, session_id),
        )

    @with_conn_lock
    async def list(self, session_id: str) -> builtins.list[BranchRecord]:
        return await self._select("session_id = ?", (session_id,))

    @with_conn_lock
    async def get_active(self, session_id: str) -> BranchRecord | None:
        rows = await self._select("session_id = ? AND is_active = 1", (session_id,))
        return rows[0] if rows else None

    @with_conn_lock
    async def create(self, record: BranchRecord) -> BranchRecord:
        stored = replace(record, is_active=True)
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            root = await self._select(
                "session_id = ? AND branch_id = ?", (record.session_id, ROOT_BRANCH_ID)
            )
            await self._deactivate_all(record.session_id)
            if not root:
                await self._insert(implicit_root(record))
            await self._insert(stored)
            await self._conn.commit()
        except BaseException:
            await self._conn.rollback()
            raise
        return stored

    @with_conn_lock
    async def set_active(self, session_id: str, branch_id: str) -> BranchRecord | None:
        now = int(time.time() * 1000)
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            found = await self._select("session_id = ? AND branch_id = ?", (session_id, branch_id))
            if not found:
                await self._conn.rollback()
                return None
            await self._deactivate_all(session_id)
            await self._conn.execute(
                "UPDATE session_branches SET is_active = 1, last_active_at = ? "
                "WHERE agent_scope = ? AND session_id = ? AND branch_id = ?",
                (now, self._agent_scope, session_id, branch_id),
            )
            await self._conn.commit()
        except BaseException:
            await self._conn.rollback()
            raise
        return replace(found[0], is_active=True, last_active_at=now)

    @with_conn_lock
    async def touch(self, session_id: str, branch_id: str) -> None:
        await self._conn.execute(
            "UPDATE session_branches SET last_active_at = ? "
            "WHERE agent_scope = ? AND session_id = ? AND branch_id = ?",
            (int(time.time() * 1000), self._agent_scope, session_id, branch_id),
        )
        await self._conn.commit()

    @with_conn_lock
    async def active_non_root(self, session_ids: Sequence[str]) -> dict[str, BranchRecord]:
        ids = builtins.list(dict.fromkeys(session_ids))
        out: dict[str, BranchRecord] = {}
        for start in range(0, len(ids), _IN_CHUNK):
            chunk = ids[start : start + _IN_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            for record in await self._select(
                f"is_active = 1 AND branch_id != ? AND session_id IN ({placeholders})",
                (ROOT_BRANCH_ID, *chunk),
            ):
                out[record.session_id] = record
        return out

    @with_conn_lock
    async def delete_session(self, session_id: str) -> builtins.list[BranchRecord]:
        rows = await self._select("session_id = ?", (session_id,))
        await self._conn.execute(
            "DELETE FROM session_branches WHERE agent_scope = ? AND session_id = ?",
            (self._agent_scope, session_id),
        )
        await self._conn.commit()
        return rows
