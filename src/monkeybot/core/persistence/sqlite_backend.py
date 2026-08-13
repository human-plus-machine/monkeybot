"""SQLite-backed StorageBackend implementation.

Lazy-imported by :func:`monkeybot.core.persistence.backends.create_storage_backend`
when a ``sqlite://`` URL is used — never loaded in Postgres-only deployments.
"""

from __future__ import annotations

import aiosqlite

from monkeybot.core.persistence.durable_runs import SQLiteRunStore
from monkeybot.core.persistence.scheduled_loops import SQLiteScheduledLoopStore
from monkeybot.core.persistence.session_turn_locks import SQLiteSessionTurnLockStore
from monkeybot.core.persistence.history import SQLiteHistoryStore
from monkeybot.core.persistence.sqlite import (
    apply_schema,
    backfill_legacy_agent_scope,
    open_connection,
)
from monkeybot.core.persistence.usage import SQLiteUsageStore


class SQLiteStorageBackend:
    """SQLite-backed storage backend. One shared connection for the process lifetime."""

    def __init__(self, db_url: str, agent_scope: str = "") -> None:
        self._db_url = db_url
        self._agent_scope = agent_scope
        self._conn: aiosqlite.Connection | None = None
        self._history_store: SQLiteHistoryStore | None = None
        self._usage_store: SQLiteUsageStore | None = None
        self._runs_store: SQLiteRunStore | None = None
        self._scheduled_loops_store: SQLiteScheduledLoopStore | None = None
        self._session_turn_lock_store: SQLiteSessionTurnLockStore | None = None

    async def open(self, *, run_schema: bool = True) -> None:
        self._conn = await open_connection(self._db_url)
        if run_schema:
            agent_scope_migrated = await apply_schema(self._conn)
            if agent_scope_migrated and self._agent_scope:
                await backfill_legacy_agent_scope(self._conn, self._agent_scope)
        self._history_store = SQLiteHistoryStore(self._conn, self._agent_scope)
        self._usage_store = SQLiteUsageStore(self._conn)
        self._runs_store = SQLiteRunStore(self._conn)
        self._scheduled_loops_store = SQLiteScheduledLoopStore(self._conn)
        self._session_turn_lock_store = SQLiteSessionTurnLockStore(self._conn)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            self._history_store = None
            self._usage_store = None
            self._runs_store = None
            self._scheduled_loops_store = None
            self._session_turn_lock_store = None

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
