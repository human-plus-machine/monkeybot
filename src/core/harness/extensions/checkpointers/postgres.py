"""Postgres-backed :class:`Checkpointer` shipped as a builtin backend.

See 1b-contracts.md §8.1.1 for the table shape. The pool is shared via
:mod:`src.core.harness.extensions._postgres_pool` — registering the same DSN
across multiple surfaces (checkpointer, memory store, job storage) therefore
uses a single pool per ``(dsn_env, schema_name)`` key.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .._postgres_pool import get_pool
from ..base import Checkpointer
from ..errors import CheckpointerError, CheckpointMissing
from ..values import CheckpointRef

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

_DDL_PATH = Path(__file__).resolve().parent.parent / "migrations" / "postgres" / "emonk_ckpt.sql"


def _new_ulid() -> str:
    """Return a monotonic-ish id: nanosecond time + UUID suffix.

    Checkpoint ids only need to be strictly-monotonic **per session**; the
    nanosecond-precision timestamp plus an 8-hex random suffix yields a sort
    key that is monotonic in practice even across concurrent writes.
    """
    return f"{time.time_ns():016x}-{uuid.uuid4().hex[:8]}"


class PostgresCheckpointer(Checkpointer):
    """Checkpointer backed by a Postgres table at ``{schema_name}.checkpoints``.

    Args:
        dsn_env: Env var name holding the full Postgres DSN.
        schema_name: Schema to create/use (default ``"emonk_ckpt"``).
        pool_min_size: Minimum pool connections.
        pool_max_size: Maximum pool connections.
        statement_timeout_ms: Per-command timeout (milliseconds). Applied by
            asyncpg as ``command_timeout`` seconds.
    """

    def __init__(
        self,
        *,
        dsn_env: str = "CKPT_DSN",
        schema_name: str = "emonk_ckpt",
        pool_min_size: int = 1,
        pool_max_size: int = 10,
        statement_timeout_ms: int = 5000,
    ) -> None:
        self.dsn_env = dsn_env
        self.schema_name = schema_name
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

    @staticmethod
    def _dumps(state: Mapping[str, Any]) -> bytes:
        import orjson

        return orjson.dumps(dict(state), default=str)

    @staticmethod
    def _loads(payload: bytes) -> Mapping[str, Any]:
        import orjson

        result: Any = orjson.loads(bytes(payload))
        if not isinstance(result, Mapping):
            raise CheckpointerError(
                f"PostgresCheckpointer expected a Mapping payload, got {type(result).__name__}"
            )
        return result

    def _uri(self, checkpoint_id: str) -> str:
        return f"postgres://{self.schema_name}/checkpoints/{checkpoint_id}"

    async def write(
        self,
        session_id: str,
        state: Mapping[str, Any],
        *,
        reason: Literal["turn_end", "pre_destructive", "manual", "rewind"] = "turn_end",
    ) -> CheckpointRef:
        """Insert a new row and return a populated :class:`CheckpointRef`."""
        pool = await self._ensure_pool()
        checkpoint_id = _new_ulid()
        payload = self._dumps(state)
        created_at = datetime.now(UTC)
        async with pool.acquire() as conn:
            await conn.execute(
                f'INSERT INTO "{self.schema_name}".checkpoints '
                "(session_id, checkpoint_id, reason, created_at, bytes, payload) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                session_id,
                checkpoint_id,
                reason,
                created_at,
                len(payload),
                payload,
            )
        return CheckpointRef(
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            reason=reason,
            created_at=created_at,
            bytes=len(payload),
            uri=self._uri(checkpoint_id),
        )

    async def read(
        self,
        session_id: str,
        checkpoint_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Return the stored state for ``checkpoint_id`` (or the latest write)."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            if checkpoint_id is None:
                row = await conn.fetchrow(
                    f'SELECT payload FROM "{self.schema_name}".checkpoints '
                    "WHERE session_id = $1 ORDER BY created_at DESC LIMIT 1",
                    session_id,
                )
                if row is None:
                    return None
                return self._loads(row["payload"])
            row = await conn.fetchrow(
                f'SELECT payload FROM "{self.schema_name}".checkpoints '
                "WHERE session_id = $1 AND checkpoint_id = $2",
                session_id,
                checkpoint_id,
            )
            if row is None:
                raise CheckpointMissing(session_id, checkpoint_id)
            return self._loads(row["payload"])

    async def list(self, session_id: str, *, limit: int = 100) -> list[CheckpointRef]:
        """Return checkpoint refs newest-first up to ``limit`` rows."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f'SELECT checkpoint_id, reason, created_at, bytes '
                f'FROM "{self.schema_name}".checkpoints '
                "WHERE session_id = $1 ORDER BY created_at DESC LIMIT $2",
                session_id,
                limit,
            )
        return [
            CheckpointRef(
                session_id=session_id,
                checkpoint_id=row["checkpoint_id"],
                reason=row["reason"],
                created_at=row["created_at"],
                bytes=int(row["bytes"]),
                uri=self._uri(row["checkpoint_id"]),
            )
            for row in rows
        ]

    async def delete_session(self, session_id: str) -> None:
        """Remove every checkpoint row belonging to ``session_id``."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f'DELETE FROM "{self.schema_name}".checkpoints WHERE session_id = $1',
                session_id,
            )

    async def gc(self, older_than: timedelta) -> int:
        """Delete checkpoints older than ``older_than`` and return the count."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f'DELETE FROM "{self.schema_name}".checkpoints '
                "WHERE created_at < now() - ($1::bigint * interval '1 second')",
                int(older_than.total_seconds()),
            )
        parts = result.split()
        try:
            return int(parts[-1])
        except (ValueError, IndexError):
            return 0


__all__ = ["PostgresCheckpointer"]
