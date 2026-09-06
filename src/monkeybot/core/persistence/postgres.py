"""PostgreSQL storage backend (requires ``pip install 'monkeybot[postgres]'``)."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import asyncpg

from monkeybot.core.llm.provider import Message, Role
from monkeybot.core.llm.usage import (
    Usage,
    UsageBreakdown,
    UsageBucket,
    UsageGranularity,
    UsageSeriesPoint,
    UsageSummary,
)
from monkeybot.core.memory.ids import outbox_id, utc_now_iso
from monkeybot.core.memory.outbox import (
    STATUS_COMMITTED,
    STATUS_DEAD,
    STATUS_PENDING,
    OutboxRow,
    backoff_iso,
    is_permanent_error,
)
from monkeybot.core.persistence.durable_runs import (
    _SUBAGENT_COLUMNS,
    SubagentEnvelope,
    SubagentRunRow,
    _tuple_to_run_row,
)
from monkeybot.core.persistence.errors import AmbiguousCommitError
from monkeybot.core.persistence.scheduled_loops import (
    _SCHEDULED_LOOP_COLUMNS,
    ScheduledLoopCreate,
    ScheduledLoopRow,
    _loop_id_from_create,
    _map_loop_tuples,
    _try_row_from_tuple,
    validate_loop_guards,
)
from monkeybot.core.persistence.thread_summary import (
    SUBAGENT_THREAD_ID_PREFIX,
    ChatThreadSummary,
    preview_from_content_blob,
)
from monkeybot.core.persistence.usage_buckets import postgres_bucket_sql
from monkeybot.core.types.content_blocks import ContentBlock

logger = logging.getLogger(__name__)

_VALID_ROLES: tuple[str, ...] = ("user", "assistant", "system")

_SCHEMA_DDLS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS conversation_history (
    id BIGSERIAL PRIMARY KEY,
    thread_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    agent_scope TEXT NOT NULL DEFAULT ''
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
    scratch_dir TEXT,
    worker_id TEXT,
    claimed_at BIGINT
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
    estimated_prompt_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0
)""",
    "CREATE INDEX IF NOT EXISTS idx_history_thread ON conversation_history(thread_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_runs_parent ON subagent_runs(parent_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_runs_status ON subagent_runs(status) WHERE status IN ('pending','running')",
    "CREATE INDEX IF NOT EXISTS idx_usage_thread ON turn_usage(thread_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_usage_cost ON turn_usage(created_at)",
    "ALTER TABLE turn_usage ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE turn_usage ADD COLUMN IF NOT EXISTS cache_creation_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE subagent_runs ADD COLUMN IF NOT EXISTS worker_id TEXT",
    "ALTER TABLE subagent_runs ADD COLUMN IF NOT EXISTS claimed_at BIGINT",
    # IF NOT EXISTS (not a separate check-then-add) so concurrent startups from
    # multiple agents against a freshly-upgraded, shared DB_URL can't race each
    # other into a duplicate-column error.
    "ALTER TABLE conversation_history ADD COLUMN IF NOT EXISTS agent_scope TEXT NOT NULL DEFAULT ''",
    """CREATE INDEX IF NOT EXISTS idx_history_scope_thread
    ON conversation_history(agent_scope, thread_id, created_at DESC, id DESC)""",
    """CREATE TABLE IF NOT EXISTS scheduled_loops (
    loop_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    prompt TEXT NOT NULL,
    interval_ms BIGINT NOT NULL,
    max_ticks INTEGER,
    max_runtime_ms BIGINT,
    skip_if_busy INTEGER NOT NULL DEFAULT 1,
    tick_index INTEGER NOT NULL DEFAULT 0,
    next_tick_at_ms BIGINT NOT NULL,
    started_at_ms BIGINT NOT NULL,
    last_tick_at_ms BIGINT,
    last_error TEXT,
    stop_reason TEXT,
    tick_in_flight INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    claimed_at_ms BIGINT
)""",
    """CREATE INDEX IF NOT EXISTS idx_scheduled_loops_due
    ON scheduled_loops(status, tick_in_flight, next_tick_at_ms)
    WHERE status = 'active'""",
    """CREATE TABLE IF NOT EXISTS session_turn_locks (
    session_id TEXT PRIMARY KEY,
    request_id TEXT,
    claimed_at_ms BIGINT
)""",
    """CREATE TABLE IF NOT EXISTS memory_outbox (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    workspace_id TEXT,
    wing TEXT NOT NULL,
    room TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    traceparent TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    palace_id TEXT NOT NULL DEFAULT ''
)""",
    "CREATE INDEX IF NOT EXISTS idx_memory_outbox_pending ON memory_outbox(agent_id, palace_id, status, created_at)",
)


async def _apply_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        for ddl in _SCHEMA_DDLS:
            await conn.execute(ddl)
        await conn.execute(
            "ALTER TABLE memory_outbox ADD COLUMN IF NOT EXISTS agent_id TEXT NOT NULL DEFAULT ''"
        )
        await conn.execute(
            "ALTER TABLE memory_outbox ADD COLUMN IF NOT EXISTS palace_id TEXT NOT NULL DEFAULT ''"
        )
        await conn.execute("ALTER TABLE conversation_history ADD COLUMN IF NOT EXISTS turn_id TEXT")
        await conn.execute(
            "ALTER TABLE conversation_history ADD COLUMN IF NOT EXISTS message_id TEXT"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_history_message_id "
            "ON conversation_history(message_id) "
            "WHERE message_id IS NOT NULL AND message_id != ''"
        )


async def _warn_if_legacy_unscoped_history(pool: asyncpg.Pool) -> None:
    """Log once if pre-migration (``agent_scope = ''``) rows remain.

    Unreachable via ``list_threads``/``load``/``reset`` until an operator
    runs, per thread_id (see ``docs/migrations/agent-scope-namespacing.md``
    for why there's no automatic backfill)::

        UPDATE conversation_history SET agent_scope = '<agent-id>'
        WHERE thread_id = '<thread-id>' AND agent_scope = '';
    """
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM conversation_history WHERE agent_scope = '')"
        )
    if exists:
        logger.warning(
            "conversation_history has rows with agent_scope='' that this agent "
            "did not write — they are not reachable via list_threads, load, or "
            "reset until an operator backfills agent_scope for each legacy "
            "thread_id by hand (see _warn_if_legacy_unscoped_history docstring "
            "for the exact UPDATE statement)."
        )


class PostgresHistoryStore:
    """asyncpg-backed conversation history store, scoped to ``agent_scope``.

    ``agent_scope`` isolates threads when one DB_URL is shared across gateways
    for different agent roots — without it, ``list_threads`` would surface
    another agent's newest transcript. Defaults to ``''`` (unscoped) for
    in-process/test callers; production gateways pass the resolved agent root.
    """

    def __init__(self, pool: asyncpg.Pool, agent_scope: str = "") -> None:
        self._pool = pool
        self._agent_scope = agent_scope

    async def _insert_message(
        self,
        conn: asyncpg.Connection,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        role = message.role
        if role not in _VALID_ROLES:
            raise ValueError(f"invalid role: {role!r}")
        if message_id:
            existing = await conn.fetchval(
                "SELECT 1 FROM conversation_history WHERE message_id = $1 LIMIT 1",
                message_id,
            )
            if existing is not None:
                return
        payload = json.dumps(
            [b.to_dict() for b in message.content],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        created_at = int(time.time() * 1000)
        await conn.execute(
            """
            INSERT INTO conversation_history(
                thread_id, role, content, created_at, agent_scope, turn_id, message_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            thread_id,
            message.role,
            payload,
            created_at,
            self._agent_scope,
            turn_id,
            message_id,
        )

    async def append(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await self._insert_message(
                conn, thread_id, message, turn_id=turn_id, message_id=message_id
            )

    async def append_with_outbox(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str,
        message_id: str,
        outbox: dict[str, Any],
    ) -> None:
        """Insert history and a pending memory outbox row in one transaction."""
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                await self._insert_message(
                    conn, thread_id, message, turn_id=turn_id, message_id=message_id
                )
                await _insert_outbox_pending(conn, **outbox)
        except (TimeoutError, OSError, ConnectionError, asyncpg.InterfaceError) as extra:
            raise AmbiguousCommitError(str(extra)) from extra

    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        async with self._pool.acquire() as conn:
            if limit is None:
                rows = await conn.fetch(
                    """
                    SELECT id, role, content
                    FROM conversation_history
                    WHERE thread_id = $1 AND agent_scope = $2
                    ORDER BY created_at ASC, id ASC
                    """,
                    thread_id,
                    self._agent_scope,
                )
                rows_chrono = list(rows)
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, role, content
                    FROM conversation_history
                    WHERE thread_id = $1 AND agent_scope = $2
                    ORDER BY created_at DESC, id DESC
                    LIMIT $3
                    """,
                    thread_id,
                    self._agent_scope,
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
                "DELETE FROM conversation_history WHERE thread_id = $1 AND agent_scope = $2",
                thread_id,
                self._agent_scope,
            )

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        """Replace thread history atomically (single transaction)."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM conversation_history WHERE thread_id = $1 AND agent_scope = $2",
                thread_id,
                self._agent_scope,
            )
            for msg in messages:
                await self._insert_message(conn, thread_id, msg)

    async def list_threads(self, limit: int = 50) -> list[ChatThreadSummary]:
        """Return recent threads, newest first, excluding subagent transcripts.

        A subagent thread_id (prefixed ``SUBAGENT_THREAD_ID_PREFIX``) that
        finishes after its parent's last turn would otherwise outrank the
        parent as "newest," making ``--continue`` resume the subagent's
        transcript under the main-agent prompt and tools instead of the
        actual previous chat.
        """
        cap = max(1, min(limit, 200))
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    h.thread_id,
                    MAX(h.created_at) AS last_message_at,
                    COUNT(*)::int AS message_count,
                    (
                        SELECT h2.content
                        FROM conversation_history h2
                        WHERE h2.thread_id = h.thread_id AND h2.agent_scope = $1
                        ORDER BY h2.created_at DESC, h2.id DESC
                        LIMIT 1
                    ) AS last_content
                FROM conversation_history h
                WHERE h.agent_scope = $1 AND h.thread_id NOT LIKE $3
                GROUP BY h.thread_id
                ORDER BY last_message_at DESC
                LIMIT $2
                """,
                self._agent_scope,
                cap,
                f"{SUBAGENT_THREAD_ID_PREFIX}%",
            )
        out: list[ChatThreadSummary] = []
        for row in rows:
            preview = preview_from_content_blob(str(row["last_content"] or ""))
            out.append(
                ChatThreadSummary(
                    thread_id=str(row["thread_id"]),
                    last_message_at=int(row["last_message_at"]),
                    message_count=int(row["message_count"]),
                    preview=preview or "(empty)",
                )
            )
        return out


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
                    estimated_prompt_tokens, cache_read_tokens, cache_creation_tokens
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
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
                usage.cache_read_tokens,
                usage.cache_creation_tokens,
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
                MAX(created_at) AS period_end_ms,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens
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
                cache_read_tokens=0,
                cache_creation_tokens=0,
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
            period_start_ms=int(row["period_start_ms"])
            if row["period_start_ms"] is not None
            else None,
            period_end_ms=int(row["period_end_ms"]) if row["period_end_ms"] is not None else None,
            last_prompt_tokens=last_pt,
            last_estimated_prompt_tokens=last_est,
            cache_read_tokens=int(row["cache_read_tokens"]),
            cache_creation_tokens=int(row["cache_creation_tokens"]),
        )

    async def breakdown(
        self,
        since_ms: int | None = None,
        *,
        bucket: UsageGranularity = "day",
    ) -> UsageBreakdown:
        """Aggregate usage by model, UTC day, and (time bucket × model)."""
        clauses: list[str] = []
        params: list[object] = []
        if since_ms is not None:
            clauses.append("created_at >= $1")
            params.append(since_ms)
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        agg = """
                COUNT(*) AS turns,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cost_usd), 0.0) AS cost_usd
        """
        model_sql = f"""
            SELECT model, {agg}
            FROM turn_usage
            {where_sql}
            GROUP BY model
            ORDER BY cost_usd DESC, model ASC
        """
        day_sql = f"""
            SELECT
                to_char(
                    to_timestamp(created_at / 1000.0) AT TIME ZONE 'UTC',
                    'YYYY-MM-DD'
                ) AS day,
                {agg}
            FROM turn_usage
            {where_sql}
            GROUP BY day
            ORDER BY day ASC
        """
        series_sql = f"""
            SELECT
                {postgres_bucket_sql(bucket)} AS bucket,
                model,
                {agg}
            FROM turn_usage
            {where_sql}
            GROUP BY bucket, model
            ORDER BY bucket ASC, cost_usd DESC, model ASC
        """

        async with self._pool.acquire() as conn:
            model_rows = await conn.fetch(model_sql, *params)
            day_rows = await conn.fetch(day_sql, *params)
            series_rows = await conn.fetch(series_sql, *params)

        return UsageBreakdown(
            by_model=[_pg_usage_bucket(row, "model") for row in model_rows],
            by_day=[_pg_usage_bucket(row, "day") for row in day_rows],
            by_bucket_model=[_pg_usage_series(row) for row in series_rows],
        )


def _pg_usage_metrics(row: asyncpg.Record) -> tuple[int, int, int, float]:
    return (
        int(row["turns"]),
        int(row["input_tokens"]),
        int(row["output_tokens"]),
        float(row["cost_usd"]),
    )


def _pg_usage_bucket(row: asyncpg.Record, key_field: str) -> UsageBucket:
    turns, input_tokens, output_tokens, cost_usd = _pg_usage_metrics(row)
    return UsageBucket(
        key=str(row[key_field]),
        turns=turns,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )


def _pg_usage_series(row: asyncpg.Record) -> UsageSeriesPoint:
    turns, input_tokens, output_tokens, cost_usd = _pg_usage_metrics(row)
    return UsageSeriesPoint(
        bucket=str(row["bucket"]),
        model=str(row["model"]),
        turns=turns,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )


class PostgresRunStore:
    """asyncpg-backed subagent run lifecycle store."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def record_pending(
        self,
        run_id: str,
        parent_run_id: str | None,
        script: str,
        envelope: SubagentEnvelope,
        scratch_dir: object,
    ) -> None:
        await self._record_run("pending", run_id, parent_run_id, script, envelope, scratch_dir)

    async def record_started(
        self,
        run_id: str,
        parent_run_id: str | None,
        script: str,
        envelope: SubagentEnvelope,
        scratch_dir: object,
    ) -> None:
        await self._record_run("running", run_id, parent_run_id, script, envelope, scratch_dir)

    async def _record_run(
        self,
        status: str,
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
                    status, result_json, error_json, started_at, finished_at, scratch_dir,
                    worker_id, claimed_at
                )
                VALUES ($1, $2, $3, $4, $5, NULL, NULL, $6, NULL, $7, NULL, NULL)
                """,
                run_id,
                parent_run_id,
                script,
                envelope.to_json(),
                status,
                now_ms,
                str(scratch_dir),
            )

    async def claim(self, run_id: str, worker_id: str) -> bool:
        now_ms = int(time.time() * 1000)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE subagent_runs
                SET status = 'running',
                    worker_id = $2,
                    claimed_at = $3
                WHERE run_id = $1 AND status = 'pending'
                RETURNING run_id
                """,
                run_id,
                worker_id,
                now_ms,
            )
        return row is not None

    async def renew_claim(self, run_id: str, worker_id: str) -> bool:
        now_ms = int(time.time() * 1000)
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE subagent_runs
                SET claimed_at = $1
                WHERE run_id = $2
                  AND status = 'running'
                  AND worker_id = $3
                """,
                now_ms,
                run_id,
                worker_id,
            )
        try:
            return int(result.split()[-1]) == 1
        except (ValueError, IndexError):
            return False

    async def list_stale_claims(self, stale_after_ms: int) -> list[SubagentRunRow]:
        cutoff = int(time.time() * 1000) - stale_after_ms
        columns = ", ".join(_SUBAGENT_COLUMNS)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {columns} FROM subagent_runs
                WHERE status = 'running'
                  AND claimed_at IS NOT NULL
                  AND claimed_at < $1
                """,
                cutoff,
            )
        return [_tuple_to_run_row(tuple(r)) for r in rows]

    async def reset_stale_claim(
        self,
        run_id: str,
        stale_after_ms: int,
        *,
        worker_id: str | None = None,
    ) -> bool:
        cutoff = int(time.time() * 1000) - stale_after_ms
        async with self._pool.acquire() as conn:
            if worker_id is None:
                result = await conn.execute(
                    """
                    UPDATE subagent_runs
                    SET status = 'pending',
                        worker_id = NULL,
                        claimed_at = NULL
                    WHERE run_id = $1
                      AND status = 'running'
                      AND claimed_at IS NOT NULL
                      AND claimed_at < $2
                    """,
                    run_id,
                    cutoff,
                )
            else:
                result = await conn.execute(
                    """
                    UPDATE subagent_runs
                    SET status = 'pending',
                        worker_id = NULL,
                        claimed_at = NULL
                    WHERE run_id = $1
                      AND status = 'running'
                      AND worker_id = $2
                      AND claimed_at IS NOT NULL
                      AND claimed_at < $3
                    """,
                    run_id,
                    worker_id,
                    cutoff,
                )
        try:
            return int(result.split()[-1]) == 1
        except (ValueError, IndexError):
            return False

    async def reset_stale_claims(self, stale_after_ms: int) -> int:
        cutoff = int(time.time() * 1000) - stale_after_ms
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE subagent_runs
                SET status = 'pending',
                    worker_id = NULL,
                    claimed_at = NULL
                WHERE status = 'running'
                  AND claimed_at IS NOT NULL
                  AND claimed_at < $1
                """,
                cutoff,
            )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def record_completed(
        self,
        run_id: str,
        result_json: str,
        *,
        worker_id: str | None = None,
    ) -> bool:
        now_ms = int(time.time() * 1000)
        async with self._pool.acquire() as conn:
            if worker_id is None:
                result = await conn.execute(
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
            else:
                result = await conn.execute(
                    """
                    UPDATE subagent_runs
                    SET status = 'completed',
                        result_json = $1,
                        finished_at = $2,
                        error_json = NULL
                    WHERE run_id = $3
                      AND status = 'running'
                      AND worker_id = $4
                    """,
                    result_json,
                    now_ms,
                    run_id,
                    worker_id,
                )
        try:
            return int(result.split()[-1]) == 1
        except (ValueError, IndexError):
            return False

    async def record_failed(
        self,
        run_id: str,
        error: str,
        *,
        worker_id: str | None = None,
    ) -> bool:
        now_ms = int(time.time() * 1000)
        err_payload = json.dumps({"message": error})
        async with self._pool.acquire() as conn:
            if worker_id is None:
                result = await conn.execute(
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
            else:
                result = await conn.execute(
                    """
                    UPDATE subagent_runs
                    SET status = 'failed',
                        error_json = $1,
                        finished_at = $2
                    WHERE run_id = $3
                      AND status = 'running'
                      AND worker_id = $4
                    """,
                    err_payload,
                    now_ms,
                    run_id,
                    worker_id,
                )
        try:
            return int(result.split()[-1]) == 1
        except (ValueError, IndexError):
            return False

    async def pending_runs(self) -> list[SubagentRunRow]:
        columns = ", ".join(_SUBAGENT_COLUMNS)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {columns} FROM subagent_runs
                WHERE status = 'pending'
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


def _asyncpg_affected_rows(status: object) -> int:
    """Parse affected-row count from an asyncpg command tag (``UPDATE 1``)."""
    try:
        return int(str(status).split()[-1])
    except (IndexError, ValueError):
        return 0


class PostgresScheduledLoopStore:
    """Postgres persistence for scheduled agent loops."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(self, spec: ScheduledLoopCreate) -> ScheduledLoopRow:
        loop_id = _loop_id_from_create(spec)
        if await self.get(loop_id) is not None:
            raise ValueError(f"scheduled loop already exists: {loop_id}")
        validate_loop_guards(
            max_ticks=spec.max_ticks,
            max_runtime_ms=spec.max_runtime_ms,
            unbounded=spec.unbounded,
        )
        now_ms = int(time.time() * 1000)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO scheduled_loops(
                    loop_id, session_id, status, prompt, interval_ms,
                    max_ticks, max_runtime_ms, skip_if_busy, tick_index,
                    next_tick_at_ms, started_at_ms, last_tick_at_ms,
                    last_error, stop_reason, tick_in_flight, worker_id, claimed_at_ms
                ) VALUES ($1, $2, 'active', $3, $4, $5, $6, $7, 0, $8, $8, NULL, NULL, NULL, 0, NULL, NULL)
                """,
                loop_id,
                spec.session_id.strip() or "loop-main",
                spec.prompt.strip(),
                spec.interval_ms,
                spec.max_ticks,
                spec.max_runtime_ms,
                1 if spec.skip_if_busy else 0,
                now_ms,
            )
        row = await self.get(loop_id)
        if row is None:
            raise RuntimeError("failed to read scheduled loop after insert")
        return row

    async def get(self, loop_id: str) -> ScheduledLoopRow | None:
        columns = ", ".join(_SCHEDULED_LOOP_COLUMNS)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {columns} FROM scheduled_loops WHERE loop_id = $1",
                loop_id,
            )
        if row is None:
            return None
        return _try_row_from_tuple(tuple(row))

    async def list_all(self) -> list[ScheduledLoopRow]:
        columns = ", ".join(_SCHEDULED_LOOP_COLUMNS)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {columns} FROM scheduled_loops ORDER BY started_at_ms DESC"
            )
        return _map_loop_tuples(rows)

    async def list_due(self, now_ms: int) -> list[ScheduledLoopRow]:
        columns = ", ".join(_SCHEDULED_LOOP_COLUMNS)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {columns} FROM scheduled_loops
                WHERE status = 'active'
                  AND tick_in_flight = 0
                  AND next_tick_at_ms <= $1
                ORDER BY next_tick_at_ms ASC
                """,
                now_ms,
            )
        return _map_loop_tuples(rows)

    async def claim_tick(self, loop_id: str, worker_id: str) -> ScheduledLoopRow | None:
        now_ms = int(time.time() * 1000)
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE scheduled_loops
                SET tick_in_flight = 1, worker_id = $1, claimed_at_ms = $2
                WHERE loop_id = $3
                  AND status = 'active'
                  AND tick_in_flight = 0
                  AND next_tick_at_ms <= $2
                """,
                worker_id,
                now_ms,
                loop_id,
            )
        if _asyncpg_affected_rows(status) != 1:
            return None
        return await self.get(loop_id)

    async def release_stale_claims(self, stale_after_ms: int) -> int:
        cutoff = int(time.time() * 1000) - stale_after_ms
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE scheduled_loops
                SET tick_in_flight = 0, worker_id = NULL, claimed_at_ms = NULL,
                    last_error = COALESCE(last_error, 'stale tick claim released')
                WHERE tick_in_flight = 1
                  AND claimed_at_ms IS NOT NULL
                  AND claimed_at_ms < $1
                """,
                cutoff,
            )
        return _asyncpg_affected_rows(status)

    async def complete_tick(
        self,
        loop_id: str,
        *,
        worker_id: str,
        error: str | None = None,
    ) -> ScheduledLoopRow | None:
        row = await self.get(loop_id)
        if row is None or not row.tick_in_flight or row.worker_id != worker_id:
            return None
        now_ms = int(time.time() * 1000)
        tick_index = row.tick_index + 1
        stop_reason: str | None = None
        status = row.status
        if error:
            status = "failed"
            stop_reason = "tick_error"
        elif row.max_ticks is not None and tick_index >= row.max_ticks:
            status = "completed"
            stop_reason = "max_ticks"
        elif row.max_runtime_ms is not None and (now_ms - row.started_at_ms) >= row.max_runtime_ms:
            status = "completed"
            stop_reason = "max_runtime"
        next_tick = now_ms + row.interval_ms if status == "active" else row.next_tick_at_ms
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE scheduled_loops
                SET tick_index = $1, last_tick_at_ms = $2, last_error = $3,
                    status = $4, stop_reason = $5, next_tick_at_ms = $6,
                    tick_in_flight = 0, worker_id = NULL, claimed_at_ms = NULL
                WHERE loop_id = $7 AND worker_id = $8 AND tick_in_flight = 1
                """,
                tick_index,
                now_ms,
                error,
                status,
                stop_reason,
                next_tick,
                loop_id,
                worker_id,
            )
        return await self.get(loop_id)

    async def defer_tick(self, loop_id: str, *, worker_id: str, reason: str) -> bool:
        row = await self.get(loop_id)
        if row is None or row.worker_id != worker_id or not row.tick_in_flight:
            return False
        now_ms = int(time.time() * 1000)
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE scheduled_loops
                SET tick_in_flight = 0, worker_id = NULL, claimed_at_ms = NULL,
                    next_tick_at_ms = $1, last_error = $2
                WHERE loop_id = $3 AND worker_id = $4 AND tick_in_flight = 1
                """,
                now_ms + row.interval_ms,
                reason,
                loop_id,
                worker_id,
            )
        return _asyncpg_affected_rows(status) == 1

    async def renew_tick_claim(self, loop_id: str, worker_id: str) -> bool:
        now_ms = int(time.time() * 1000)
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE scheduled_loops
                SET claimed_at_ms = $1
                WHERE loop_id = $2
                  AND worker_id = $3
                  AND tick_in_flight = 1
                """,
                now_ms,
                loop_id,
                worker_id,
            )
        return _asyncpg_affected_rows(status) == 1

    async def pause(self, loop_id: str) -> bool:
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE scheduled_loops
                SET status = 'paused', tick_in_flight = 0, worker_id = NULL, claimed_at_ms = NULL
                WHERE loop_id = $1
                """,
                loop_id,
            )
        return _asyncpg_affected_rows(status) == 1

    async def resume(self, loop_id: str) -> bool:
        now_ms = int(time.time() * 1000)
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE scheduled_loops
                SET status = 'active', stop_reason = NULL, next_tick_at_ms = $1,
                    tick_in_flight = 0, worker_id = NULL, claimed_at_ms = NULL
                WHERE loop_id = $2 AND status = 'paused'
                """,
                now_ms,
                loop_id,
            )
        return _asyncpg_affected_rows(status) == 1

    async def stop(self, loop_id: str, *, stop_reason: str = "manual") -> bool:
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE scheduled_loops
                SET status = 'completed', stop_reason = $1,
                    tick_in_flight = 0, worker_id = NULL, claimed_at_ms = NULL
                WHERE loop_id = $2
                """,
                stop_reason,
                loop_id,
            )
        return _asyncpg_affected_rows(status) == 1


class PostgresSessionTurnLockStore:
    """Postgres-backed exclusive turn lock per session."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def release_stale_claims(self, stale_after_ms: int) -> int:
        cutoff = int(time.time() * 1000) - stale_after_ms
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE session_turn_locks
                SET request_id = NULL, claimed_at_ms = NULL
                WHERE request_id IS NOT NULL
                  AND claimed_at_ms IS NOT NULL
                  AND claimed_at_ms < $1
                """,
                cutoff,
            )
        return int(status.split()[-1])

    async def try_acquire(self, session_id: str, request_id: str) -> bool:
        from monkeybot.core.persistence.session_turn_locks import session_turn_stale_ms

        await self.release_stale_claims(session_turn_stale_ms())
        now_ms = int(time.time() * 1000)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE session_turn_locks
                SET request_id = $2, claimed_at_ms = $3
                WHERE session_id = $1 AND request_id IS NULL
                RETURNING session_id
                """,
                session_id,
                request_id,
                now_ms,
            )
            if row is not None:
                return True
            try:
                await conn.execute(
                    """
                    INSERT INTO session_turn_locks (session_id, request_id, claimed_at_ms)
                    VALUES ($1, $2, $3)
                    """,
                    session_id,
                    request_id,
                    now_ms,
                )
                return True
            except asyncpg.UniqueViolationError:
                return False

    async def release(self, session_id: str, request_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE session_turn_locks
                SET request_id = NULL, claimed_at_ms = NULL
                WHERE session_id = $1 AND request_id = $2
                """,
                session_id,
                request_id,
            )

    async def is_busy(self, session_id: str) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1 FROM session_turn_locks
                WHERE session_id = $1 AND request_id IS NOT NULL
                LIMIT 1
                """,
                session_id,
            )
        return row is not None


async def _insert_outbox_pending(
    conn: asyncpg.Connection,
    *,
    agent_id: str,
    thread_id: str,
    turn_id: str,
    message_id: str,
    role: str,
    content: str,
    workspace_id: str | None,
    wing: str,
    room: str,
    created_at: str | None = None,
    traceparent: str | None = None,
    palace_id: str = "",
) -> str | None:
    row_id = outbox_id(agent_id=agent_id, thread_id=thread_id, message_id=message_id, role=role)
    existing = await conn.fetchval("SELECT status FROM memory_outbox WHERE id = $1", row_id)
    if existing is not None:
        return None if str(existing) == STATUS_COMMITTED else row_id
    await conn.execute(
        """
        INSERT INTO memory_outbox (
          id, agent_id, thread_id, turn_id, message_id, role, content,
          workspace_id, wing, room, created_at, status, attempts, traceparent,
          palace_id
        ) VALUES (
          $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'pending', 0, $12, $13
        )
        """,
        row_id,
        agent_id,
        thread_id,
        turn_id,
        message_id,
        role,
        content,
        workspace_id,
        wing,
        room,
        created_at or utc_now_iso(),
        traceparent,
        palace_id,
    )
    return row_id


class PostgresOutboxStore:
    """Postgres-backed memory outbox (same table shape as SQLite)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def insert_pending(
        self,
        *,
        agent_id: str,
        thread_id: str,
        turn_id: str,
        message_id: str,
        role: str,
        content: str,
        workspace_id: str | None,
        wing: str,
        room: str,
        created_at: str | None = None,
        traceparent: str | None = None,
        palace_id: str = "",
        commit: bool = True,
    ) -> str | None:
        del commit
        async with self._pool.acquire() as conn:
            return await _insert_outbox_pending(
                conn,
                agent_id=agent_id,
                thread_id=thread_id,
                turn_id=turn_id,
                message_id=message_id,
                role=role,
                content=content,
                workspace_id=workspace_id,
                wing=wing,
                room=room,
                created_at=created_at,
                traceparent=traceparent,
                palace_id=palace_id,
            )

    async def claim_batch(
        self,
        *,
        agent_id: str,
        lease_owner: str,
        limit: int = 16,
        lease_seconds: int = 30,
        palace_id: str = "",
    ) -> list[Any]:
        now = datetime.now(UTC)
        now_iso = now.isoformat(timespec="seconds")
        expires = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                    UPDATE memory_outbox
                    SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL
                    WHERE status = 'processing'
                      AND lease_expires_at IS NOT NULL AND lease_expires_at < $1
                      AND agent_id = $2
                    """,
                now_iso,
                agent_id,
            )
            rows = await conn.fetch(
                """
                    SELECT id, thread_id, turn_id, message_id, role, content, workspace_id,
                           wing, room, created_at, status, attempts, next_attempt_at,
                           last_error, traceparent, lease_owner, lease_expires_at, agent_id,
                           palace_id
                    FROM memory_outbox
                    WHERE status = 'pending'
                      AND agent_id = $1
                      AND (palace_id = $2 OR palace_id = '' OR palace_id IS NULL)
                      AND (next_attempt_at IS NULL OR next_attempt_at <= $3)
                    ORDER BY created_at ASC
                    LIMIT $4
                    FOR UPDATE SKIP LOCKED
                    """,
                agent_id,
                palace_id,
                now_iso,
                limit,
            )
            claimed: list[OutboxRow] = []
            for raw in rows:
                await conn.execute(
                    """
                        UPDATE memory_outbox
                        SET status = 'processing', lease_owner = $1, lease_expires_at = $2,
                            attempts = attempts + 1,
                            palace_id = CASE
                                WHEN palace_id IS NULL OR palace_id = '' THEN $3
                                ELSE palace_id
                            END
                        WHERE id = $4
                        """,
                    lease_owner,
                    expires,
                    palace_id,
                    raw["id"],
                )
                claimed.append(
                    OutboxRow(
                        id=str(raw["id"]),
                        thread_id=str(raw["thread_id"]),
                        turn_id=str(raw["turn_id"]),
                        message_id=str(raw["message_id"]),
                        role=str(raw["role"]),
                        content=raw["content"],
                        workspace_id=raw["workspace_id"],
                        wing=str(raw["wing"]),
                        room=str(raw["room"]),
                        created_at=str(raw["created_at"]),
                        status=str(raw["status"]),
                        attempts=int(raw["attempts"] or 0),
                        next_attempt_at=raw["next_attempt_at"],
                        last_error=raw["last_error"],
                        traceparent=raw["traceparent"],
                        lease_owner=raw["lease_owner"],
                        lease_expires_at=raw["lease_expires_at"],
                        agent_id=str(raw["agent_id"] or ""),
                        palace_id=str(raw["palace_id"] or ""),
                    )
                )
        return claimed

    async def mark_committed(self, row_ids: list[str], *, lease_owner: str | None = None) -> int:
        if not row_ids:
            return 0
        async with self._pool.acquire() as conn:
            if lease_owner:
                result = await conn.execute(
                    """
                    UPDATE memory_outbox
                    SET status = 'committed', lease_owner = NULL, lease_expires_at = NULL,
                        last_error = NULL, next_attempt_at = NULL
                    WHERE id = ANY($1::text[]) AND lease_owner = $2
                    """,
                    row_ids,
                    lease_owner,
                )
            else:
                result = await conn.execute(
                    """
                    UPDATE memory_outbox
                    SET status = 'committed', lease_owner = NULL, lease_expires_at = NULL,
                        last_error = NULL, next_attempt_at = NULL
                    WHERE id = ANY($1::text[])
                    """,
                    row_ids,
                )
        try:
            return int(str(result).split()[-1])
        except (ValueError, IndexError):
            return 0

    async def mark_retry(
        self,
        row_id: str,
        *,
        error_class: str,
        attempts: int,
        permanent: bool | None = None,
        lease_owner: str | None = None,
    ) -> int:
        dead = bool(permanent) if permanent is not None else is_permanent_error(error_class)
        status = STATUS_DEAD if dead else STATUS_PENDING
        next_at = None if status == STATUS_DEAD else backoff_iso(attempts)
        async with self._pool.acquire() as conn:
            if lease_owner:
                result = await conn.execute(
                    """
                    UPDATE memory_outbox
                    SET status = $1, last_error = $2, next_attempt_at = $3,
                        lease_owner = NULL, lease_expires_at = NULL
                    WHERE id = $4 AND lease_owner = $5
                    """,
                    status,
                    error_class,
                    next_at,
                    row_id,
                    lease_owner,
                )
            else:
                result = await conn.execute(
                    """
                    UPDATE memory_outbox
                    SET status = $1, last_error = $2, next_attempt_at = $3,
                        lease_owner = NULL, lease_expires_at = NULL
                    WHERE id = $4
                    """,
                    status,
                    error_class,
                    next_at,
                    row_id,
                )
        try:
            return int(str(result).split()[-1])
        except (ValueError, IndexError):
            return 0

    async def gc_committed(self, *, days: int = 7) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM memory_outbox
                WHERE status = 'committed' AND created_at < $1
                """,
                cutoff,
            )
        # asyncpg returns "UPDATE N"
        try:
            return int(str(result).split()[-1])
        except (ValueError, IndexError):
            return 0

    async def pending_depth(self, *, agent_id: str | None = None) -> tuple[int, float]:
        async with self._pool.acquire() as conn:
            if agent_id:
                row = await conn.fetchrow(
                    """
                    SELECT COUNT(*), MIN(created_at)
                    FROM memory_outbox
                    WHERE status IN ('pending', 'processing') AND agent_id = $1
                    """,
                    agent_id,
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT COUNT(*), MIN(created_at)
                    FROM memory_outbox
                    WHERE status IN ('pending', 'processing')
                    """
                )
        count = int(row[0] or 0) if row else 0
        oldest = row[1] if row else None
        age = 0.0
        if oldest:
            try:
                created = datetime.fromisoformat(str(oldest))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                age = max(0.0, (datetime.now(UTC) - created).total_seconds())
            except ValueError:
                age = 0.0
        return count, age

    async def dead_depth(self, *, agent_id: str | None = None) -> int:
        async with self._pool.acquire() as conn:
            if agent_id:
                val = await conn.fetchval(
                    "SELECT COUNT(*) FROM memory_outbox WHERE status = 'dead' AND agent_id = $1",
                    agent_id,
                )
            else:
                val = await conn.fetchval(
                    "SELECT COUNT(*) FROM memory_outbox WHERE status = 'dead'"
                )
        return int(val or 0)

    async def close(self) -> None:
        return


class PostgresStorageBackend:
    """Postgres-backed storage backend using an asyncpg connection pool."""

    shares_outbox = True

    def __init__(self, db_url: str, agent_scope: str = "") -> None:
        self._db_url = db_url
        self._agent_scope = agent_scope
        self._pool: asyncpg.Pool | None = None
        self._history_store: PostgresHistoryStore | None = None
        self._usage_store: PostgresUsageStore | None = None
        self._runs_store: PostgresRunStore | None = None
        self._scheduled_loops_store: PostgresScheduledLoopStore | None = None
        self._session_turn_lock_store: PostgresSessionTurnLockStore | None = None
        self._outbox_store: PostgresOutboxStore | None = None

    async def open(self, *, run_schema: bool = True) -> None:
        min_size = int(os.environ.get("POSTGRES_POOL_MIN", "1"))
        max_size = int(os.environ.get("POSTGRES_POOL_MAX", "5"))
        self._pool = await asyncpg.create_pool(self._db_url, min_size=min_size, max_size=max_size)
        if run_schema:
            await _apply_schema(self._pool)
            if self._agent_scope:
                await _warn_if_legacy_unscoped_history(self._pool)
        self._history_store = PostgresHistoryStore(self._pool, self._agent_scope)
        self._usage_store = PostgresUsageStore(self._pool)
        self._runs_store = PostgresRunStore(self._pool)
        self._scheduled_loops_store = PostgresScheduledLoopStore(self._pool)
        logger.warning("goal ledger unavailable on Postgres; SQLite is the durable backend")
        self._session_turn_lock_store = PostgresSessionTurnLockStore(self._pool)
        self._outbox_store = PostgresOutboxStore(self._pool)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._history_store = None
            self._usage_store = None
            self._runs_store = None
            self._scheduled_loops_store = None
            self._session_turn_lock_store = None
            self._outbox_store = None

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

    def scheduled_loops(self) -> PostgresScheduledLoopStore:
        if self._scheduled_loops_store is None:
            raise RuntimeError("PostgresStorageBackend.open() has not been called")
        return self._scheduled_loops_store

    def goal_ledger(self) -> None:
        return None

    def session_turns(self) -> PostgresSessionTurnLockStore:
        if self._session_turn_lock_store is None:
            raise RuntimeError("PostgresStorageBackend.open() has not been called")
        return self._session_turn_lock_store

    def outbox(self) -> PostgresOutboxStore:
        if self._outbox_store is None:
            raise RuntimeError("PostgresStorageBackend.open() has not been called")
        return self._outbox_store
