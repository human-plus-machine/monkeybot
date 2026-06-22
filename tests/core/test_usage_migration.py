"""SQLite migration and round-trip tests for cache token columns."""

from __future__ import annotations

import pytest
import pytest_asyncio

from monkeybot.core.llm.usage import Usage
from monkeybot.core.persistence.sqlite import apply_schema, open_connection
from monkeybot.core.persistence.usage import SQLiteUsageStore

_LEGACY_TURN_USAGE_DDL = """
CREATE TABLE turn_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    run_id TEXT,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cached_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    duration_ms INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    context_json TEXT,
    estimated_prompt_tokens INTEGER NOT NULL DEFAULT 0
)
"""


async def _table_column_names(conn) -> set[str]:
    cur = await conn.execute("PRAGMA table_info(turn_usage)")
    rows = await cur.fetchall()
    await cur.close()
    return {str(r[1]) for r in rows}


@pytest_asyncio.fixture
async def usage_conn():
    conn = await open_connection("sqlite:///:memory:")
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_apply_schema_adds_cache_columns_to_legacy_db(usage_conn) -> None:
    await usage_conn.execute(_LEGACY_TURN_USAGE_DDL)
    await usage_conn.commit()
    names_before = await _table_column_names(usage_conn)
    assert "cache_read_tokens" not in names_before
    assert "cache_creation_tokens" not in names_before

    await apply_schema(usage_conn)

    names_after = await _table_column_names(usage_conn)
    assert "cache_read_tokens" in names_after
    assert "cache_creation_tokens" in names_after


@pytest.mark.asyncio
async def test_apply_schema_is_idempotent(usage_conn) -> None:
    await usage_conn.execute(_LEGACY_TURN_USAGE_DDL)
    await usage_conn.commit()
    await apply_schema(usage_conn)
    names_once = await _table_column_names(usage_conn)
    await apply_schema(usage_conn)
    names_twice = await _table_column_names(usage_conn)
    assert names_once == names_twice
    assert "cache_read_tokens" in names_twice
    assert "cache_creation_tokens" in names_twice


@pytest.mark.asyncio
async def test_record_and_summary_roundtrip_cache_columns(usage_conn) -> None:
    await apply_schema(usage_conn)
    store = SQLiteUsageStore(usage_conn)
    row = Usage(
        input_tokens=1,
        output_tokens=1,
        cached_tokens=14,
        cache_read_tokens=10,
        cache_creation_tokens=4,
        cost_usd=0.0,
        duration_ms=1,
    )
    await store.record("thr", "gpt-5", row)
    await store.record("thr", "gpt-5", row)

    summary = await store.summary(thread_id="thr")
    assert summary.cache_read_tokens == 20
    assert summary.cache_creation_tokens == 8
    assert summary.cached_tokens == 28


@pytest.mark.asyncio
async def test_legacy_rows_read_zero_after_migration(usage_conn) -> None:
    await usage_conn.execute(_LEGACY_TURN_USAGE_DDL)
    await usage_conn.execute(
        """
        INSERT INTO turn_usage(
            thread_id, run_id, model,
            input_tokens, output_tokens, cached_tokens,
            cost_usd, duration_ms, created_at, context_json,
            estimated_prompt_tokens
        )
        VALUES ('legacy', NULL, 'gemini', 5, 2, 3, 0.1, 1, 1000, NULL, 0)
        """
    )
    await usage_conn.commit()
    await apply_schema(usage_conn)

    store = SQLiteUsageStore(usage_conn)
    summary = await store.summary(thread_id="legacy")
    assert summary.turns == 1
    assert summary.cache_read_tokens == 0
    assert summary.cache_creation_tokens == 0
