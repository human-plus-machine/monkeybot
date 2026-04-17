"""Postgres-backed :class:`JobStorage` shipped as a builtin backend.

See 1b-contracts.md §3.3 / §8.1.3. Jobs live in the
``{schema_name}.jobs`` table; ``claim_job`` uses ``SELECT … FOR UPDATE
SKIP LOCKED`` inside a transaction so concurrent callers never observe
the same "unleased" row twice (JOB-C-01).

The asyncpg pool is shared via
:mod:`src.core.harness.extensions._postgres_pool` — registering the same
DSN across surfaces reuses one pool per ``(dsn_env, schema_name)``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._postgres_pool import get_pool
from ..base import JobStorage
from ..errors import BackendConfigError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncpg

_DDL_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "postgres"
    / "emonk_scheduler.sql"
)


class PostgresJobStorage(JobStorage):
    """ABC-conformant :class:`JobStorage` backed by Postgres.

    Args:
        dsn_env: Environment variable holding the Postgres DSN (default
            ``"SCHED_DSN"``).
        schema_name: Schema owning the ``jobs`` table (default
            ``"emonk_scheduler"``).
        pool_min_size: Minimum asyncpg pool size.
        pool_max_size: Maximum asyncpg pool size.
        statement_timeout_ms: Per-command timeout (milliseconds).
    """

    def __init__(
        self,
        *,
        dsn_env: str = "SCHED_DSN",
        schema_name: str = "emonk_scheduler",
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
    def _job_id(job: Mapping[str, Any]) -> str:
        if "job_id" in job:
            return str(job["job_id"])
        if "id" in job:
            return str(job["id"])
        raise BackendConfigError(
            "PostgresJobStorage: every job must contain a 'job_id' (or 'id') field"
        )

    @staticmethod
    def _dumps(value: Mapping[str, Any]) -> str:
        import orjson

        return orjson.dumps(dict(value), default=str).decode()

    @staticmethod
    def _loads(raw: Any) -> dict[str, Any]:
        import orjson

        if raw is None:
            return {}
        if isinstance(raw, bytes | bytearray):
            data: Any = orjson.loads(bytes(raw))
        elif isinstance(raw, str):
            data = orjson.loads(raw)
        else:
            data = raw
        return dict(data) if isinstance(data, Mapping) else {"value": data}

    def _row_to_job(self, row: Mapping[str, Any]) -> dict[str, Any]:
        payload = self._loads(row.get("payload"))
        job: dict[str, Any] = dict(payload)
        job["job_id"] = row["id"]
        job["status"] = row.get("status")
        leased_until = row.get("leased_until")
        job["leased_until"] = (
            leased_until.isoformat() if isinstance(leased_until, datetime) else None
        )
        job["lease_token"] = row.get("lease_token")
        return job

    async def load_jobs(self) -> list[Mapping[str, Any]]:
        """Return every row in ``{schema}.jobs`` as a dict."""
        pool = await self._ensure_pool()
        sql = (
            f'SELECT id, payload, status, leased_until, lease_token '
            f'FROM "{self.schema_name}".jobs'
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql)
        return [self._row_to_job(row) for row in rows]

    async def save_jobs(self, jobs: Sequence[Mapping[str, Any]]) -> None:
        """Replace the ``jobs`` table with ``jobs`` (delete + batch insert)."""
        new_jobs = [(self._job_id(job), dict(job)) for job in jobs]
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(f'DELETE FROM "{self.schema_name}".jobs')
            if not new_jobs:
                return
            insert_sql = (
                f'INSERT INTO "{self.schema_name}".jobs '
                f"(id, payload, status) "
                f"VALUES ($1, $2::jsonb, $3)"
            )
            for jid, job in new_jobs:
                payload = self._dumps(
                    {
                        k: v
                        for k, v in job.items()
                        if k
                        not in {
                            "id",
                            "job_id",
                            "status",
                            "leased_until",
                            "lease_token",
                        }
                    }
                )
                status = str(job.get("status", "pending"))
                await conn.execute(insert_sql, jid, payload, status)

    async def claim_job(
        self, job_id: str, lease_duration_seconds: int = 300
    ) -> bool:
        """Atomically claim ``job_id`` via ``FOR UPDATE SKIP LOCKED``."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f'SELECT id, leased_until FROM "{self.schema_name}".jobs '
                f"WHERE id = $1 FOR UPDATE SKIP LOCKED",
                job_id,
            )
            now = datetime.now(UTC)
            lease_until = now + timedelta(seconds=lease_duration_seconds)
            if row is None:
                inserted = await conn.fetchrow(
                    f'INSERT INTO "{self.schema_name}".jobs '
                    f"(id, payload, status, leased_until, lease_token) "
                    f"VALUES ($1, $2::jsonb, 'pending', $3, $4) "
                    f"ON CONFLICT (id) DO NOTHING "
                    f"RETURNING id",
                    job_id,
                    self._dumps({}),
                    lease_until,
                    str(uuid.uuid4()),
                )
                return inserted is not None
            existing_until = row["leased_until"]
            if existing_until is not None and existing_until > now:
                return False
            await conn.execute(
                f'UPDATE "{self.schema_name}".jobs '
                f"SET leased_until = $1, lease_token = $2, updated_at = now() "
                f"WHERE id = $3",
                lease_until,
                str(uuid.uuid4()),
                job_id,
            )
            return True

    async def release_job(self, job_id: str) -> None:
        """Clear the lease on ``job_id`` (no-op if row missing)."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f'UPDATE "{self.schema_name}".jobs '
                f"SET leased_until = NULL, lease_token = NULL, updated_at = now() "
                f"WHERE id = $1",
                job_id,
            )

    async def get_job(self, job_id: str) -> Mapping[str, Any] | None:
        """Return the document for ``job_id`` or ``None`` if missing."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f'SELECT id, payload, status, leased_until, lease_token '
                f'FROM "{self.schema_name}".jobs WHERE id = $1',
                job_id,
            )
        if row is None:
            return None
        return self._row_to_job(row)


__all__ = ["PostgresJobStorage"]
