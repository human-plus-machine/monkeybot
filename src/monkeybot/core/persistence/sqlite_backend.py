"""SQLite-backed StorageBackend implementation.

Lazy-imported by :func:`monkeybot.core.persistence.backends.create_storage_backend`
when a ``sqlite://`` URL is used — never loaded in Postgres-only deployments.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite

from monkeybot.core.persistence.durable_runs import SQLiteRunStore
from monkeybot.core.persistence.history import SQLiteHistoryStore
from monkeybot.core.persistence.scheduled_loops import SQLiteScheduledLoopStore
from monkeybot.core.persistence.session_turn_locks import SQLiteSessionTurnLockStore
from monkeybot.core.persistence.sqlite import (
    TaskReentrantLock,
    apply_schema,
    backfill_legacy_agent_scope,
    db_owned_by_agent_root,
    open_connection,
    warn_if_legacy_unscoped_history,
)
from monkeybot.core.persistence.usage import SQLiteUsageStore


class SQLiteStorageBackend:
    """SQLite-backed storage backend. One shared connection for the process lifetime."""

    shares_outbox = False

    def __init__(self, db_url: str, agent_scope: str = "", agent_root: Path | None = None) -> None:
        self._db_url = db_url
        self._agent_scope = agent_scope
        self._agent_root = agent_root
        self._conn: aiosqlite.Connection | None = None
        self._tx_lock = TaskReentrantLock()
        self._history_store: SQLiteHistoryStore | None = None
        self._usage_store: SQLiteUsageStore | None = None
        self._runs_store: SQLiteRunStore | None = None
        self._scheduled_loops_store: SQLiteScheduledLoopStore | None = None
        self._session_turn_lock_store: SQLiteSessionTurnLockStore | None = None
        self._outbox_store: Any | None = None

    async def open(self, *, run_schema: bool = True) -> None:
        self._conn = await open_connection(self._db_url)
        if run_schema:
            await apply_schema(self._conn)
            if self._agent_scope:
                if db_owned_by_agent_root(self._db_url, self._agent_root):
                    await backfill_legacy_agent_scope(self._conn, self._agent_scope)
                else:
                    await warn_if_legacy_unscoped_history(self._conn)
        self._history_store = SQLiteHistoryStore(
            self._conn, agent_scope=self._agent_scope, lock=self._tx_lock
        )
        self._usage_store = SQLiteUsageStore(self._conn, lock=self._tx_lock)
        self._runs_store = SQLiteRunStore(self._conn, lock=self._tx_lock)
        self._scheduled_loops_store = SQLiteScheduledLoopStore(self._conn, lock=self._tx_lock)
        self._session_turn_lock_store = SQLiteSessionTurnLockStore(self._conn, lock=self._tx_lock)
        from monkeybot.core.memory.outbox import SqliteOutboxStore

        self._outbox_store = SqliteOutboxStore(
            self._conn, owns_connection=False, lock=self._tx_lock
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            self._history_store = None
            self._usage_store = None
            self._runs_store = None
            self._scheduled_loops_store = None
            self._session_turn_lock_store = None
            self._outbox_store = None

    def history(self) -> SQLiteHistoryStore:
        if self._history_store is None:
            raise RuntimeError("SQLiteStorageBackend.open() has not been called")
        return self._history_store

    def usage(self) -> SQLiteUsageStore:
        if self._usage_store is None:
            raise RuntimeError("SQLiteStorageBackend.open() has not been called")
        return self._usage_store

    def runs(self) -> SQLiteRunStore:
        if self._runs_store is None:
            raise RuntimeError("SQLiteStorageBackend.open() has not been called")
        return self._runs_store

    def scheduled_loops(self) -> SQLiteScheduledLoopStore:
        if self._scheduled_loops_store is None:
            raise RuntimeError("SQLiteStorageBackend.open() has not been called")
        return self._scheduled_loops_store

    def session_turns(self) -> SQLiteSessionTurnLockStore:
        if self._session_turn_lock_store is None:
            raise RuntimeError("SQLiteStorageBackend.open() has not been called")
        return self._session_turn_lock_store

    def outbox(self) -> Any:
        if self._outbox_store is None:
            raise RuntimeError("SQLiteStorageBackend.open() has not been called")
        return self._outbox_store
