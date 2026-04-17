"""Postgres-backed :class:`IdentitySource` (Story 5).

Table shape (see 1b-contracts.md §8.1.4):

    CREATE TABLE <schema>.files (
        principal_id TEXT,
        file_name    TEXT,
        content      TEXT NOT NULL DEFAULT '',
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (principal_id, file_name)
    );

The shared ``_postgres_pool`` helper applies the idempotent DDL at
``migrations/postgres/emonk_identity.sql`` on first use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._postgres_pool import get_pool
from ..base import IdentitySource
from ..errors import IdentityNotFound
from ..values import LoadedIdentity, MemoryPatch

if TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncpg

    from ...events import Principal

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "postgres"
_DDL_PATH = _MIGRATIONS_DIR / "emonk_identity.sql"


_FILE_TO_ATTR: dict[str, str] = {
    "SOUL.md": "soul",
    "RULES.md": "rules",
    "IDENTITY.md": "identity",
    "USER.md": "user",
    "INDEX.md": "index",
    "MEMORY.md": "memory",
    "HEARTBEAT.md": "heartbeat",
}


class PostgresIdentitySource(IdentitySource):
    """ABC-conformant :class:`IdentitySource` backed by a Postgres table.

    Args:
        dsn_env: Environment variable holding the Postgres DSN.
        schema_name: Schema owning the ``files`` table.
        cache_ttl_seconds: TTL advertised on the returned identity.
        pool_min_size / pool_max_size / statement_timeout_ms: Forwarded to
            the shared :func:`get_pool` helper.
    """

    def __init__(
        self,
        *,
        dsn_env: str = "IDENTITY_DSN",
        schema_name: str = "emonk_identity",
        cache_ttl_seconds: int = 300,
        pool_min_size: int = 1,
        pool_max_size: int = 10,
        statement_timeout_ms: int = 5000,
    ) -> None:
        self.dsn_env = dsn_env
        self.schema_name = schema_name
        self.cache_ttl_seconds = cache_ttl_seconds
        self._pool: asyncpg.Pool | None = None
        self._pool_min_size = pool_min_size
        self._pool_max_size = pool_max_size
        self._command_timeout = statement_timeout_ms / 1000.0

    async def _ensure_pool(self) -> Any:
        if self._pool is None:
            self._pool = await get_pool(
                dsn_env=self.dsn_env,
                schema_name=self.schema_name,
                ddl_path=_DDL_PATH,
                min_size=self._pool_min_size,
                max_size=self._pool_max_size,
                command_timeout=self._command_timeout,
            )
        return self._pool

    async def load(
        self,
        *,
        principal: Principal,
        session_id: str | None = None,
    ) -> LoadedIdentity:
        """Return the identity rows for ``principal`` mapped into a :class:`LoadedIdentity`."""
        pool = await self._ensure_pool()
        sql = (
            f'SELECT file_name, content FROM "{self.schema_name}".files '
            "WHERE principal_id = $1"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, principal.id)
        if not rows:
            raise IdentityNotFound(principal.id)

        values: dict[str, str] = dict.fromkeys(_FILE_TO_ATTR.values(), "")
        extras: dict[str, str] = {}
        for row in rows:
            file_name = str(row["file_name"])
            attr = _FILE_TO_ATTR.get(file_name)
            content = str(row["content"] or "")
            if attr is None:
                extras[f"extra_{file_name}"] = content
                continue
            values[attr] = content
            for _file_name, attr in _FILE_TO_ATTR.items():
                if (attr not in values or values[attr] == "") and not any(
                    str(row["file_name"]) == _file_name for row in rows
                ):
                    extras[f"missing_{_file_name}"] = "1"

        return LoadedIdentity(
            principal_id=principal.id,
            session_id=session_id,
            soul=values["soul"],
            rules=values["rules"],
            identity=values["identity"],
            user=values["user"],
            index=values["index"],
            memory=values["memory"],
            heartbeat=values["heartbeat"],
            loaded_at=datetime.now(UTC),
            ttl_seconds=self.cache_ttl_seconds,
            source_backend="postgres",
            extras=extras,
        )

    async def write_memory(
        self,
        *,
        principal: Principal,
        patch: MemoryPatch,
    ) -> None:
        """Upsert or delete the principal's MEMORY.md / HEARTBEAT.md row."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            if patch.operation == "delete":
                await conn.execute(
                    f'DELETE FROM "{self.schema_name}".files '
                    "WHERE principal_id = $1 AND file_name = $2",
                    principal.id,
                    patch.target,
                )
                return
            if patch.operation == "append":
                row = await conn.fetchrow(
                    f'SELECT content FROM "{self.schema_name}".files '
                    "WHERE principal_id = $1 AND file_name = $2",
                    principal.id,
                    patch.target,
                )
                existing = str(row["content"]) if row is not None else ""
                new_content = existing + (patch.content or "")
            else:
                new_content = patch.content or ""
            await conn.execute(
                f'INSERT INTO "{self.schema_name}".files '
                "(principal_id, file_name, content, updated_at) "
                "VALUES ($1, $2, $3, now()) "
                "ON CONFLICT (principal_id, file_name) DO UPDATE SET "
                "content = EXCLUDED.content, updated_at = EXCLUDED.updated_at",
                principal.id,
                patch.target,
                new_content,
            )


__all__ = ["PostgresIdentitySource"]
