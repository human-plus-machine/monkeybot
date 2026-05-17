"""SQLite-backed StorageBackend implementation.

Lazy-imported by :func:`monkeybot.core.persistence.backends.create_storage_backend`
when a ``sqlite://`` URL is used — never loaded in Postgres-only deployments.
"""

from __future__ import annotations

import aiosqlite

from monkeybot.core.persistence.history import SQLiteHistoryStore
from monkeybot.core.persistence.usage import SQLiteUsageStore
from monkeybot.core.persistence.durable_runs import SQLiteRunStore
from monkeybot.core.persistence.sqlite import apply_schema, open_connection


class SQLiteStorageBackend:
    """SQLite-backed storage backend. One shared connection for the process lifetime."""

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url
        self._conn: aiosqlite.Connection | None = None
        self._history_store: SQLiteHistoryStore | None = None
        self._usage_store: SQLiteUsageStore | None = None
        self._runs_store: SQLiteRunStore | None = None

    async def open(self) -> None:
        self._conn = await open_connection(self._db_url)
        await apply_schema(self._conn)
        self._history_store = SQLiteHistoryStore(self._conn)
        self._usage_store = SQLiteUsageStore(self._conn)
        self._runs_store = SQLiteRunStore(self._conn)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            self._history_store = None
            self._usage_store = None
            self._runs_store = None

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
