"""PostgreSQL storage backend (requires ``pip install 'monkeybot[postgres]'``)."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import cast

import asyncpg

from monkeybot.core.llm.usage import Usage, UsageSummary
from monkeybot.core.llm.provider import Message, Role
from monkeybot.core.types.content_blocks import ContentBlock
from monkeybot.core.persistence.durable_runs import (
    SubagentEnvelope,
    SubagentRunRow,
    _SUBAGENT_COLUMNS,
    _tuple_to_run_row,
)

logger = logging.getLogger(__name__)

_VALID_ROLES: tuple[str, ...] = ("user", "assistant", "system")

_SCHEMA_DDLS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS conversation_history (
    id BIGSERIAL PRIMARY KEY,
    thread_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at BIGINT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS subagent_runs (
    run_id TEXT PRIMARY KEY,
    parent_run_id TEXT,
    script TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    started_at BIGINT,
    finished_at BIGINT,
    scratch_dir TEXT
)""",
    """CREATE TABLE IF NOT EXISTS turn_usage (
    id BIGSERIAL PRIMARY KEY,
    thread_id TEXT NOT NULL,
    run_id TEXT,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cached_tokens INTEGER NOT NULL,
    cost_usd DOUBLE PRECISION NOT NULL,
    duration_ms INTEGER NOT NULL,
    created_at BIGINT NOT NULL,
    context_json TEXT,
    estimated_prompt_tokens INTEGER NOT NULL DEFAULT 0
)""",
    "CREATE INDEX IF NOT EXISTS idx_history_thread ON conversation_history(thread_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_runs_parent ON subagent_runs(parent_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_runs_status ON subagent_runs(status) WHERE status IN ('pending','running')",
    "CREATE INDEX IF NOT EXISTS idx_usage_thread ON turn_usage(thread_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_usage_cost ON turn_usage(created_at)",
)


async def _apply_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        for ddl in _SCHEMA_DDLS:
            await conn.execute(ddl)


class PostgresHistoryStore:
    """asyncpg-backed conversation history store."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def _insert_message(self, conn: asyncpg.Connection, thread_id: str, message: Message) -> None:
        role = message.role
        if role not in _VALID_ROLES:
            raise ValueError(f"invalid role: {role!r}")
        payload = json.dumps(
            [b.to_dict() for b in message.content],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        created_at = int(time.time() * 1000)
        await conn.execute(
            """
            INSERT INTO conversation_history(thread_id, role, content, created_at)
            VALUES ($1, $2, $3, $4)
            """,
            thread_id,
            message.role,
            payload,
            created_at,
        )

    async def append(self, thread_id: str, message: Message) -> None:
        async with self._pool.acquire() as conn:
            await self._insert_message(conn, thread_id, message)

    async def load(self, thread_id: str, limit: int = 100) -> list[Message]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, role, content
                FROM conversation_history
                WHERE thread_id = $1
                ORDER BY created_at DESC, id DESC
                LIMIT $2
                """,
                thread_id,
                limit,
            )
        rows_chrono = list(reversed(rows))
        out: list[Message] = []
        for row in rows_chrono:
            row_id = int(row["id"])
            role = row["role"]
            content_blob = row["content"]
            try:
                raw = json.loads(content_blob)
                if not isinstance(raw, list):
                    raise ValueError("stored content must be a JSON array")
                blocks = [ContentBlock.from_dict(b) for b in raw]
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                logger.error(
                    "Unparseable history row id=%s thread_id=%s",
                    row_id,
                    thread_id,
                    exc_info=True,
                )
                raise ValueError(f"history row {row_id} unparseable: {exc}") from exc
            if role not in _VALID_ROLES:
                raise ValueError(f"history row {row_id} has invalid role: {role!r}")
            out.append(Message(role=cast(Role, role), content=blocks))
        return out

    async def clear(self, thread_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM conversation_history WHERE thread_id = $1", thread_id
            )

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        """Replace thread history atomically (single transaction)."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM conversation_history WHERE thread_id = $1", thread_id
                )
                for msg in messages:
                    await self._insert_message(conn, thread_id, msg)


class PostgresUsageStore:
    """asyncpg-backed usage store."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def record(
        self,
        thread_id: str,
        model: str,
        usage: Usage,
        run_id: str | None = None,
        *,
        context_json: str | None = None,
    ) -> None:
        now_ms = int(time.time() * 1000)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO turn_usage(
                    thread_id, run_id, model,
                    input_tokens, output_tokens, cached_tokens,
                    cost_usd, duration_ms, created_at, context_json,
                    estimated_prompt_tokens
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                thread_id,
                run_id,
                model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_tokens,
                usage.cost_usd,
                usage.duration_ms,
                now_ms,
                context_json,
                usage.estimated_prompt_tokens,
            )

    async def summary(
        self,
        thread_id: str | None = None,
        since_ms: int | None = None,
    ) -> UsageSummary:
        clauses: list[str] = []
        params: list[object] = []
        idx = 1
        if thread_id is not None:
            clauses.append(f"thread_id = ${idx}")
            params.append(thread_id)
            idx += 1
        if since_ms is not None:
            clauses.append(f"created_at >= ${idx}")
            params.append(since_ms)
            idx += 1
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        sql = f"""
            SELECT
                COUNT(*) AS turns,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                MIN(created_at) AS period_start_ms,
                MAX(created_at) AS period_end_ms
            FROM turn_usage
            {where_sql}
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(sql, *params)

        if row is None:
            raise RuntimeError("usage summary query returned no row")

        turns = int(row["turns"])
        if turns == 0:
            return UsageSummary(
                turns=0,
                input_tokens=0,
                output_tokens=0,
                cached_tokens=0,
                cost_usd=0.0,
                period_start_ms=None,
                period_end_ms=None,
                last_prompt_tokens=0,
                last_estimated_prompt_tokens=0,
            )

        last_pt = 0
        last_est = 0
        if thread_id is not None:
            lp_clauses: list[str] = ["thread_id = $1"]
            lp_params: list[object] = [thread_id]
            if since_ms is not None:
                lp_clauses.append("created_at >= $2")
                lp_params.append(since_ms)
            lp_where = "WHERE " + " AND ".join(lp_clauses)
            async with self._pool.acquire() as conn:
                row2 = await conn.fetchrow(
                    f"""
                    SELECT input_tokens, estimated_prompt_tokens FROM turn_usage
                    {lp_where}
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    *lp_params,
                )
            if row2 is not None:
                last_pt = int(row2["input_tokens"])
                last_est = int(row2["estimated_prompt_tokens"])

        return UsageSummary(
            turns=turns,
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            cached_tokens=int(row["cached_tokens"]),
            cost_usd=float(row["cost_usd"]),
            period_start_ms=int(row["period_start_ms"]) if row["period_start_ms"] is not None else None,
            period_end_ms=int(row["period_end_ms"]) if row["period_end_ms"] is not None else None,
            last_prompt_tokens=last_pt,
            last_estimated_prompt_tokens=last_est,
        )


class PostgresRunStore:
    """asyncpg-backed subagent run lifecycle store."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def record_started(
        self,
        run_id: str,
        parent_run_id: str | None,
        script: str,
        envelope: SubagentEnvelope,
        scratch_dir: object,
    ) -> None:
        now_ms = int(time.time() * 1000)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO subagent_runs(
                    run_id, parent_run_id, script, envelope_json,
                    status, result_json, error_json, started_at, finished_at, scratch_dir
                )
                VALUES ($1, $2, $3, $4, 'running', NULL, NULL, $5, NULL, $6)
                """,
                run_id,
                parent_run_id,
                script,
                envelope.to_json(),
                now_ms,
                str(scratch_dir),
            )

    async def record_completed(self, run_id: str, result_json: str) -> None:
        now_ms = int(time.time() * 1000)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE subagent_runs
                SET status = 'completed',
                    result_json = $1,
                    finished_at = $2,
                    error_json = NULL
                WHERE run_id = $3
                """,
                result_json,
                now_ms,
                run_id,
            )

    async def record_failed(self, run_id: str, error: str) -> None:
        now_ms = int(time.time() * 1000)
        err_payload = json.dumps({"message": error})
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE subagent_runs
                SET status = 'failed',
                    error_json = $1,
                    finished_at = $2
                WHERE run_id = $3
                """,
                err_payload,
                now_ms,
                run_id,
            )

    async def pending_runs(self) -> list[SubagentRunRow]:
        columns = ", ".join(_SUBAGENT_COLUMNS)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {columns} FROM subagent_runs
                WHERE status IN ('pending','running')
                ORDER BY started_at ASC
                """
            )
        return [_tuple_to_run_row(tuple(row)) for row in rows]

    async def get_run(self, run_id: str) -> SubagentRunRow | None:
        columns = ", ".join(_SUBAGENT_COLUMNS)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {columns} FROM subagent_runs WHERE run_id = $1",
                run_id,
            )
        if row is None:
            return None
        return _tuple_to_run_row(tuple(row))


class PostgresStorageBackend:
    """Postgres-backed storage backend using an asyncpg connection pool."""

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url
        self._pool: asyncpg.Pool | None = None
        self._history_store: PostgresHistoryStore | None = None
        self._usage_store: PostgresUsageStore | None = None
        self._runs_store: PostgresRunStore | None = None

    async def open(self) -> None:
        min_size = int(os.environ.get("POSTGRES_POOL_MIN", "1"))
        max_size = int(os.environ.get("POSTGRES_POOL_MAX", "5"))
        self._pool = await asyncpg.create_pool(
            self._db_url, min_size=min_size, max_size=max_size
        )
        await _apply_schema(self._pool)
        self._history_store = PostgresHistoryStore(self._pool)
        self._usage_store = PostgresUsageStore(self._pool)
        self._runs_store = PostgresRunStore(self._pool)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._history_store = None
            self._usage_store = None
            self._runs_store = None

    def history(self) -> PostgresHistoryStore:
        if self._history_store is None:
            raise RuntimeError("PostgresStorageBackend.open() has not been called")
        return self._history_store

    def usage(self) -> PostgresUsageStore:
        if self._usage_store is None:
            raise RuntimeError("PostgresStorageBackend.open() has not been called")
        return self._usage_store

    def runs(self) -> PostgresRunStore:
        if self._runs_store is None:
            raise RuntimeError("PostgresStorageBackend.open() has not been called")
        return self._runs_store
