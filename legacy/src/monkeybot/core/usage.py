from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
import ulid

from monkeybot.core.events import TurnComplete

log = logging.getLogger("monkeybot.usage")


@dataclass
class UsageSummary:
    """Aggregated token usage and cost statistics.

    Attributes:
        turns: Number of turns in the window.
        input_tokens: Total input tokens consumed.
        output_tokens: Total output tokens generated.
        cached_tokens: Total tokens served from cache.
        total_cost_usd: Total estimated cost in US dollars.
        avg_latency_ms: Average turn duration in milliseconds.
        since_hours: The look-back window used for the query.
    """

    turns: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_cost_usd: float
    avg_latency_ms: float
    since_hours: float


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS turn_usage (
    id            TEXT    PRIMARY KEY,
    run_id        TEXT    NOT NULL UNIQUE,
    session_id    TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL    NOT NULL DEFAULT 0.0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
)"""

_CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_turn_usage_created ON turn_usage(created_at)"


async def _init_db(db: aiosqlite.Connection) -> None:
    """Apply WAL pragma and ensure schema exists."""
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute(_CREATE_TABLE)
    await db.execute(_CREATE_INDEX)


async def record_usage(db_path: str, session_id: str, event: TurnComplete) -> None:
    """Persist a TurnComplete event as a usage row.

    Uses INSERT OR IGNORE so replaying the same run_id is a no-op.

    Args:
        db_path: Filesystem path to the SQLite database file.
        session_id: Identifier for the current session.
        event: The TurnComplete event emitted by the loop.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        async with aiosqlite.connect(db_path) as db:
            await _init_db(db)
            await db.execute(
                "INSERT OR IGNORE INTO turn_usage "
                "(id, run_id, session_id, input_tokens, output_tokens, cached_tokens, "
                "cost_usd, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(ulid.new()),
                    event.run_id,
                    session_id,
                    event.input_tokens,
                    event.output_tokens,
                    event.cached_tokens,
                    event.cost_usd,  # NOTE: cost_usd is 0.0 until providers implement cost models
                    event.duration_ms,
                    int(time.time() * 1000),
                ),
            )
            await db.commit()
        log.debug("usage recorded run_id=%s", event.run_id)
    except Exception:
        log.error("failed to record usage run_id=%s", event.run_id, exc_info=True)
        raise


async def get_usage_summary(db_path: str, since_hours: float) -> UsageSummary:
    """Return aggregated usage statistics over the last *since_hours* hours.

    Args:
        db_path: Filesystem path to the SQLite database file.
        since_hours: Look-back window in hours.

    Returns:
        A UsageSummary with aggregated counts and costs.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    since_ms = int((time.time() - since_hours * 3600) * 1000)
    async with aiosqlite.connect(db_path) as db:
        await _init_db(db)
        await db.commit()
        async with db.execute(
            "SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cached_tokens), "
            "SUM(cost_usd), AVG(duration_ms) FROM turn_usage WHERE created_at >= ?",
            (since_ms,),
        ) as cur:
            row = await cur.fetchone()

    if row is None or row[0] == 0:
        return UsageSummary(
            turns=0,
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            total_cost_usd=0.0,
            avg_latency_ms=0.0,
            since_hours=since_hours,
        )

    return UsageSummary(
        turns=int(row[0]),
        input_tokens=int(row[1] or 0),
        output_tokens=int(row[2] or 0),
        cached_tokens=int(row[3] or 0),
        total_cost_usd=float(row[4] or 0.0),
        avg_latency_ms=float(row[5] or 0.0),
        since_hours=since_hours,
    )
