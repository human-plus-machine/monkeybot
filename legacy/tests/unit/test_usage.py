from __future__ import annotations

import time

import aiosqlite
import pytest

from monkeybot.core.events import TurnComplete
from monkeybot.core.usage import get_usage_summary, record_usage


@pytest.fixture
def db_path(tmp_path: pytest.TempPathFactory) -> str:
    """Temporary SQLite database path."""
    return str(tmp_path / "test.db")


def make_event(
    run_id: str = "RUN1",
    input_tokens: int = 100,
    output_tokens: int = 50,
    duration_ms: int = 200,
) -> TurnComplete:
    """Build a TurnComplete event with sensible defaults."""
    return TurnComplete(
        run_id=run_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
    )


async def test_record_inserts_row(db_path: str) -> None:
    """A recorded event shows up in the summary."""
    await record_usage(db_path, "s1", make_event())
    summary = await get_usage_summary(db_path, 1)
    assert summary.turns == 1
    assert summary.input_tokens == 100


async def test_record_idempotent(db_path: str) -> None:
    """Recording the same run_id twice only persists one row."""
    await record_usage(db_path, "s1", make_event(run_id="RUN1"))
    await record_usage(db_path, "s1", make_event(run_id="RUN1"))
    summary = await get_usage_summary(db_path, 1)
    assert summary.turns == 1


async def test_summary_empty_returns_zeros(db_path: str) -> None:
    """A fresh database returns a zero-filled UsageSummary."""
    summary = await get_usage_summary(db_path, 24)
    assert summary.turns == 0
    assert summary.input_tokens == 0
    assert summary.output_tokens == 0
    assert summary.cached_tokens == 0
    assert summary.total_cost_usd == 0.0
    assert summary.avg_latency_ms == 0.0


async def test_summary_aggregates_correctly(db_path: str) -> None:
    """Multiple rows are correctly summed."""
    await record_usage(db_path, "s1", make_event("R1", input_tokens=100))
    await record_usage(db_path, "s1", make_event("R2", input_tokens=200))
    await record_usage(db_path, "s1", make_event("R3", input_tokens=300))
    summary = await get_usage_summary(db_path, 1)
    assert summary.turns == 3
    assert summary.input_tokens == 600


async def test_summary_since_filter(db_path: str) -> None:
    """Rows older than the look-back window are excluded."""
    await record_usage(db_path, "s1", make_event("OLD", input_tokens=999))
    # backdate the OLD row to 48 hours ago
    old_ts = int((time.time() - 48 * 3600) * 1000)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE turn_usage SET created_at=? WHERE run_id='OLD'", (old_ts,))
        await db.commit()

    await record_usage(db_path, "s1", make_event("NEW", input_tokens=50))
    summary = await get_usage_summary(db_path, 24)
    assert summary.turns == 1
    assert summary.input_tokens == 50
