"""Tests for usage accounting."""

from __future__ import annotations

import pytest
import pytest_asyncio

from monkeybot.core.llm.usage import Usage, UsageSummary, usage_from_totals
from monkeybot.core.persistence.sqlite import apply_schema, open_connection
from monkeybot.core.persistence.usage import SQLiteUsageStore
from monkeybot.core.runtime.events import UsageTotals


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
    store = SQLiteUsageStore(usage_conn)
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
    assert summary.last_estimated_prompt_tokens == 0


@pytest.mark.asyncio
async def test_usage_summary_all_threads(usage_conn) -> None:
    store = SQLiteUsageStore(usage_conn)
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
    assert summary.last_estimated_prompt_tokens == 0


@pytest.mark.asyncio
async def test_usage_record_preserves_context_json(usage_conn) -> None:
    store = SQLiteUsageStore(usage_conn)
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
async def test_usage_record_stores_estimated_prompt_tokens(usage_conn) -> None:
    store = SQLiteUsageStore(usage_conn)
    await store.record(
        "thr",
        "gemini",
        Usage(
            input_tokens=10,
            output_tokens=1,
            cached_tokens=0,
            cost_usd=0.0,
            duration_ms=1,
            estimated_prompt_tokens=4242,
        ),
    )
    cur = await usage_conn.execute(
        "SELECT estimated_prompt_tokens FROM turn_usage WHERE thread_id = ?",
        ("thr",),
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None
    assert int(row[0]) == 4242


@pytest.mark.asyncio
async def test_usage_summary_last_estimated_prompt_tokens_most_recent(usage_conn) -> None:
    store = SQLiteUsageStore(usage_conn)
    for created_at, inp, est in [(1000, 50, 500), (2000, 120, 1200), (3000, 99, 999)]:
        await usage_conn.execute(
            """
            INSERT INTO turn_usage(
                thread_id, run_id, model,
                input_tokens, output_tokens, cached_tokens,
                cost_usd, duration_ms, created_at, context_json,
                estimated_prompt_tokens
            )
            VALUES ('est1', NULL, 'gemini', ?, 1, 0, 0.0, 1, ?, NULL, ?)
            """,
            (inp, created_at, est),
        )
    await usage_conn.commit()
    summary = await store.summary(thread_id="est1")
    assert summary.last_prompt_tokens == 99
    assert summary.last_estimated_prompt_tokens == 999


@pytest.mark.asyncio
async def test_usage_summary_last_prompt_tokens_most_recent(usage_conn) -> None:
    store = SQLiteUsageStore(usage_conn)
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
    assert summary.last_estimated_prompt_tokens == 0


def test_usage_from_totals_maps_turn_complete_fields() -> None:
    u = usage_from_totals(
        UsageTotals(
            input_tokens=1,
            output_tokens=2,
            cached_tokens=3,
            cost_usd=0.5,
            duration_ms=100,
            estimated_prompt_tokens=99,
        )
    )
    assert isinstance(u, Usage)
    assert u.input_tokens == 1
    assert u.output_tokens == 2
    assert u.cached_tokens == 3
    assert u.cost_usd == 0.5
    assert u.duration_ms == 100
    assert u.estimated_prompt_tokens == 99


def test_usage_defaults_cache_fields_zero() -> None:
    u = Usage()
    assert u.cache_read_tokens == 0
    assert u.cache_creation_tokens == 0


def test_usage_from_totals_threads_cache_fields() -> None:
    u = usage_from_totals(
        UsageTotals(
            cache_read_tokens=10,
            cache_creation_tokens=4,
            cached_tokens=14,
        )
    )
    assert u.cache_read_tokens == 10
    assert u.cache_creation_tokens == 4
    assert u.cached_tokens == 14


def test_usage_summary_defaults_cache_fields_zero() -> None:
    s = UsageSummary(
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
    assert s.cache_read_tokens == 0
    assert s.cache_creation_tokens == 0


@pytest.mark.asyncio
async def test_usage_breakdown_by_model_and_day(usage_conn) -> None:
    from datetime import datetime, timezone

    store = SQLiteUsageStore(usage_conn)
    day1 = int(datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    day2 = int(datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    rows = [
        ("a", "gemini-2.5-flash", 10, 5, 0.02, day1),
        ("b", "gemini-2.5-flash", 20, 5, 0.03, day1),
        ("c", "claude-sonnet-4", 100, 50, 0.10, day2),
    ]
    for tid, model, inp, out, cost, created in rows:
        await usage_conn.execute(
            """
            INSERT INTO turn_usage(
                thread_id, run_id, model,
                input_tokens, output_tokens, cached_tokens,
                cost_usd, duration_ms, created_at, context_json
            )
            VALUES (?, NULL, ?, ?, ?, 0, ?, 1, ?, NULL)
            """,
            (tid, model, inp, out, cost, created),
        )
    await usage_conn.commit()

    breakdown = await store.breakdown()
    assert [b.key for b in breakdown.by_model] == ["claude-sonnet-4", "gemini-2.5-flash"]
    flash = breakdown.by_model[1]
    assert flash.turns == 2
    assert flash.input_tokens == 30
    assert flash.cost_usd == pytest.approx(0.05)
    assert [b.key for b in breakdown.by_day] == ["2026-07-25", "2026-07-26"]
    assert breakdown.by_day[0].turns == 2
    assert breakdown.by_day[1].cost_usd == pytest.approx(0.10)

    filtered = await store.breakdown(since_ms=day2)
    assert len(filtered.by_model) == 1
    assert filtered.by_model[0].key == "claude-sonnet-4"
    assert len(filtered.by_day) == 1
    assert filtered.by_day[0].key == "2026-07-26"
