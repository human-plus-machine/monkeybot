"""SQLite-backed usage store."""

from __future__ import annotations

import time

import aiosqlite

from monkeybot.core.llm.usage import Usage, UsageSummary


class SQLiteUsageStore:
    """Insert ``turn_usage`` rows and compute summaries."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def record(
        self,
        thread_id: str,
        model: str,
        usage: Usage,
        run_id: str | None = None,
        *,
        context_json: str | None = None,
    ) -> None:
        """Persist one usage row at ``created_at`` = now (ms)."""
        now_ms = int(time.time() * 1000)
        await self._conn.execute(
            """
            INSERT INTO turn_usage(
                thread_id, run_id, model,
                input_tokens, output_tokens, cached_tokens,
                cost_usd, duration_ms, created_at, context_json,
                estimated_prompt_tokens, cache_read_tokens, cache_creation_tokens
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
            ),
        )
        await self._conn.commit()

    async def summary(
        self,
        thread_id: str | None = None,
        since_ms: int | None = None,
    ) -> UsageSummary:
        """Aggregate totals over matching rows."""
        clauses: list[str] = []
        params: list[object] = []
        if thread_id is not None:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if since_ms is not None:
            clauses.append("created_at >= ?")
            params.append(since_ms)
        where_sql = ""
        if clauses:
            where_sql = "WHERE " + " AND ".join(clauses)

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

        cursor = await self._conn.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise RuntimeError("usage summary query returned no row")

        turns = int(row[0])
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

        period_start = row[5]
        period_end = row[6]
        last_pt = 0
        last_est = 0
        if thread_id is not None:
            lp_clauses: list[str] = ["thread_id = ?"]
            lp_params: list[object] = [thread_id]
            if since_ms is not None:
                lp_clauses.append("created_at >= ?")
                lp_params.append(since_ms)
            lp_where = "WHERE " + " AND ".join(lp_clauses)
            cur2 = await self._conn.execute(
                f"""
                SELECT input_tokens, estimated_prompt_tokens FROM turn_usage
                {lp_where}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                lp_params,
            )
            row2 = await cur2.fetchone()
            await cur2.close()
            if row2 is not None:
                last_pt = int(row2[0])
                last_est = int(row2[1])
        return UsageSummary(
            turns=turns,
            input_tokens=int(row[1]),
            output_tokens=int(row[2]),
            cached_tokens=int(row[3]),
            cost_usd=float(row[4]),
            period_start_ms=int(period_start) if period_start is not None else None,
            period_end_ms=int(period_end) if period_end is not None else None,
            last_prompt_tokens=last_pt,
            last_estimated_prompt_tokens=last_est,
            cache_read_tokens=int(row[7]),
            cache_creation_tokens=int(row[8]),
        )
