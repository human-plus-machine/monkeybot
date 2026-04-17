"""Postgres-backed :class:`MemoryStore` shipped as a builtin backend.

See 1b-contracts.md §3.2 and §8.1.2 for the table shape. The pool is shared
with :class:`PostgresCheckpointer` via :mod:`_postgres_pool` — distinct
schemas keep the ``checkpoints`` and ``items`` tables isolated.

``enable_pgvector=True`` opportunistically runs
``emonk_memory_pgvector.sql`` on top of the base DDL to add the
``embedding`` column + ivfflat index; the capability is reported through
:meth:`capabilities`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._postgres_pool import get_pool
from ..base import MemoryStore
from ..values import Item, MemoryStoreCapabilities

if TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncpg
    from langgraph.store.base import BaseStore

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "postgres"
_BASE_DDL_PATH = _MIGRATIONS_DIR / "emonk_memory.sql"
_PGVECTOR_DDL_PATH = _MIGRATIONS_DIR / "emonk_memory_pgvector.sql"


class PostgresMemoryStore(MemoryStore):
    """ABC-conformant :class:`MemoryStore` backed by a Postgres table.

    Args:
        dsn_env: Env var name holding the Postgres DSN (shared with the
            :class:`PostgresCheckpointer` by default).
        schema_name: Schema owning the ``items`` table (default
            ``"emonk_memory"``).
        pool_min_size: Minimum asyncpg pool size.
        pool_max_size: Maximum asyncpg pool size.
        statement_timeout_ms: Per-command timeout (milliseconds).
        enable_pgvector: When ``True``, the ``pgvector`` extension, embedding
            column, and ivfflat index are created on top of the base DDL and
            :meth:`capabilities` reports ``vector_search=True``.
    """

    def __init__(
        self,
        *,
        dsn_env: str = "CKPT_DSN",
        schema_name: str = "emonk_memory",
        pool_min_size: int = 1,
        pool_max_size: int = 10,
        statement_timeout_ms: int = 5000,
        enable_pgvector: bool = False,
    ) -> None:
        self.dsn_env = dsn_env
        self.schema_name = schema_name
        self.enable_pgvector = enable_pgvector
        self._pool: asyncpg.Pool | None = None
        self._pgvector_ready = False
        self._pool_min_size = pool_min_size
        self._pool_max_size = pool_max_size
        self._command_timeout = statement_timeout_ms / 1000.0

    async def _ensure_pool(self) -> Any:
        if self._pool is None:
            self._pool = await get_pool(
                dsn_env=self.dsn_env,
                schema_name=self.schema_name,
                ddl_path=_BASE_DDL_PATH,
                min_size=self._pool_min_size,
                max_size=self._pool_max_size,
                command_timeout=self._command_timeout,
            )
        if self.enable_pgvector and not self._pgvector_ready:
            ddl = _PGVECTOR_DDL_PATH.read_text().replace("__SCHEMA__", self.schema_name)
            async with self._pool.acquire() as conn:
                await conn.execute(ddl)
            self._pgvector_ready = True
        return self._pool

    async def put(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: Mapping[str, Any],
        *,
        ttl: timedelta | None = None,
    ) -> None:
        """Upsert ``(namespace, key) → value`` preserving ``created_at`` on update."""
        pool = await self._ensure_pool()
        payload = json.dumps(dict(value), default=str)
        namespace_list = list(namespace)
        now = datetime.now(UTC)
        expires_at = now + ttl if ttl is not None else None
        sql = (
            f'INSERT INTO "{self.schema_name}".items '
            "(namespace, key, value, created_at, updated_at, expires_at) "
            "VALUES ($1, $2, $3::jsonb, $4, $4, $5) "
            "ON CONFLICT (namespace, key) DO UPDATE SET "
            "value = EXCLUDED.value, "
            "updated_at = EXCLUDED.updated_at, "
            "expires_at = EXCLUDED.expires_at"
        )
        async with pool.acquire() as conn:
            await conn.execute(sql, namespace_list, key, payload, now, expires_at)

    async def get(self, namespace: tuple[str, ...], key: str) -> Item | None:
        """Return the :class:`Item` at ``(namespace, key)`` or ``None``/expired."""
        pool = await self._ensure_pool()
        sql = (
            "SELECT namespace, key, value, created_at, updated_at, expires_at "
            f'FROM "{self.schema_name}".items '
            "WHERE namespace = $1 AND key = $2"
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, list(namespace), key)
        if row is None:
            return None
        return _row_to_item(row)

    async def search(
        self,
        namespace: tuple[str, ...],
        *,
        query: str | None = None,
        filter: Mapping[str, Any] | None = None,
        limit: int = 10,
    ) -> list[Item]:
        """Return live rows under ``namespace`` matching ``filter`` / ``query``."""
        pool = await self._ensure_pool()
        clauses = ["namespace = $1", "(expires_at IS NULL OR expires_at > now())"]
        args: list[Any] = [list(namespace)]
        if filter:
            args.append(json.dumps(dict(filter), default=str))
            clauses.append(f"value @> ${len(args)}::jsonb")
        if query is not None:
            args.append(f"%{query}%")
            clauses.append(f"value::text ILIKE ${len(args)}")
        args.append(limit)
        sql = (
            "SELECT namespace, key, value, created_at, updated_at, expires_at "
            f'FROM "{self.schema_name}".items '
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY updated_at DESC LIMIT ${len(args)}"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [_row_to_item(row) for row in rows]

    async def delete(self, namespace: tuple[str, ...], key: str) -> None:
        """Delete the row at ``(namespace, key)`` if present."""
        pool = await self._ensure_pool()
        sql = f'DELETE FROM "{self.schema_name}".items WHERE namespace = $1 AND key = $2'
        async with pool.acquire() as conn:
            await conn.execute(sql, list(namespace), key)

    async def list_namespaces(
        self, prefix: tuple[str, ...] = ()
    ) -> list[tuple[str, ...]]:
        """Return distinct live namespaces whose first elements equal ``prefix``."""
        pool = await self._ensure_pool()
        prefix_list = list(prefix)
        sql = (
            "SELECT DISTINCT namespace "
            f'FROM "{self.schema_name}".items '
            "WHERE (expires_at IS NULL OR expires_at > now()) "
            "AND namespace[1:$2] = $1::text[] "
            "AND array_length(namespace, 1) >= $2"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, prefix_list, len(prefix_list))
        namespaces = [tuple(row["namespace"]) for row in rows]
        return sorted(namespaces)

    def capabilities(self) -> MemoryStoreCapabilities:
        """Declared capabilities — ``vector_search`` flips with ``enable_pgvector``."""
        return MemoryStoreCapabilities(
            vector_search=self.enable_pgvector,
            keyword_search=True,
            namespace_listing=True,
            ttl=True,
            transactional=True,
        )

    def as_langgraph_store(self) -> BaseStore:
        """Return a LangGraph :class:`BaseStore` adapter bound to this store."""
        from ._langgraph_adapter import as_langgraph_store

        return as_langgraph_store(self)


def _row_to_item(row: Mapping[str, Any]) -> Item:
    raw_value = row["value"]
    if isinstance(raw_value, str | bytes | bytearray):
        value: Any = json.loads(raw_value)
    else:
        value = raw_value
    namespace = tuple(row["namespace"])
    created_at: datetime = row["created_at"]
    updated_at: datetime = row["updated_at"]
    return Item(
        value=dict(value) if isinstance(value, Mapping) else {"value": value},
        key=str(row["key"]),
        namespace=namespace,
        created_at=created_at,
        updated_at=updated_at,
    )


__all__ = ["PostgresMemoryStore"]
