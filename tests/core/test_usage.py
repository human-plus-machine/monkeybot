"""Tests for usage accounting."""

from __future__ import annotations

import pytest
import pytest_asyncio
from monkeybot.core.db import apply_schema, open_connection
from monkeybot.core.usage import Usage, UsageStore, UsageSummary


async def _apply_schema(conn) -> None:
    await apply_schema(conn)


@pytest_asyncio.fixture
async def usage_conn():
    conn = await open_connection("sqlite:///:memory:")
    await _apply_schema(conn)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_usage_summary_aggregates_with_since_ms(usage_conn) -> None:
    store = UsageStore(usage_conn)
    cutoff = 1_000
    rows = [
        ("t", None, "gemini", 1, 1, 0, 0.1, 1, 500, None),
        ("t", None, "gemini", 1, 1, 0, 0.2, 1, 900, None),
        ("t", None, "gemini", 1, 1, 0, 0.4, 1, 1100, None),
    ]
    for row in rows:
        await usage_conn.execute(
            """
            INSERT INTO turn_usage(
                thread_id, run_id, model,
                input_tokens, output_tokens, cached_tokens,
                cost_usd, duration_ms, created_at, context_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
    await usage_conn.commit()

    summary = await store.summary(thread_id="t", since_ms=cutoff)
    assert isinstance(summary, UsageSummary)
    assert summary.turns == 1
    assert summary.input_tokens == 1
    assert summary.cost_usd == pytest.approx(0.4)
    assert summary.period_start_ms == 1100
    assert summary.period_end_ms == 1100
    assert summary.last_prompt_tokens == 1


@pytest.mark.asyncio
async def test_usage_summary_all_threads(usage_conn) -> None:
    store = UsageStore(usage_conn)
    for tid, cost in [("a", 0.05), ("b", 0.15)]:
        await usage_conn.execute(
            """
            INSERT INTO turn_usage(
                thread_id, run_id, model,
                input_tokens, output_tokens, cached_tokens,
                cost_usd, duration_ms, created_at, context_json
            )
            VALUES (?, NULL, 'gemini', 1, 1, 0, ?, 1, 1, NULL)
            """,
            (tid, cost),
        )
    await usage_conn.commit()
    summary = await store.summary(thread_id=None)
    assert isinstance(summary, UsageSummary)
    assert summary.turns == 2
    assert summary.cost_usd == pytest.approx(0.2)
    assert summary.last_prompt_tokens == 0


@pytest.mark.asyncio
async def test_usage_record_preserves_context_json(usage_conn) -> None:
    store = UsageStore(usage_conn)
    ctx = '{"tool":"run_command"}'
    await store.record(
        "thr",
        "gemini",
        Usage(input_tokens=1, output_tokens=1, cached_tokens=0, cost_usd=0.0, duration_ms=1),
        context_json=ctx,
    )
    cursor = await usage_conn.execute(
        "SELECT context_json FROM turn_usage WHERE thread_id = ?",
        ("thr",),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    assert row[0] == ctx


@pytest.mark.asyncio
async def test_usage_summary_last_prompt_tokens_most_recent(usage_conn) -> None:
    store = UsageStore(usage_conn)
    for created_at, inp in [(1000, 50), (2000, 120), (3000, 99)]:
        await usage_conn.execute(
            """
            INSERT INTO turn_usage(
                thread_id, run_id, model,
                input_tokens, output_tokens, cached_tokens,
                cost_usd, duration_ms, created_at, context_json
            )
            VALUES ('thr', NULL, 'gemini', ?, 1, 0, 0.0, 1, ?, NULL)
            """,
            (inp, created_at),
        )
    await usage_conn.commit()
    summary = await store.summary(thread_id="thr")
    assert isinstance(summary, UsageSummary)
    assert summary.turns == 3
    assert summary.last_prompt_tokens == 99
