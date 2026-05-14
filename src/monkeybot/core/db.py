"""SQLite connection helpers and schema DDL for MonkeyBot persistence."""

from __future__ import annotations

import os
from typing import Final

import aiosqlite

DEFAULT_DB_URL: Final[str] = "sqlite:///data/monkeybot.db"

SCHEMA_DDLS: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS conversation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_call_id TEXT,
    tool_name TEXT,
    created_at INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS subagent_runs (
    run_id TEXT PRIMARY KEY,
    parent_run_id TEXT,
    script TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    started_at INTEGER,
    finished_at INTEGER,
    scratch_dir TEXT
)""",
    """CREATE TABLE IF NOT EXISTS turn_usage (
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
    context_json TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_history_thread ON conversation_history(thread_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_runs_parent ON subagent_runs(parent_run_id)",
    """CREATE INDEX IF NOT EXISTS idx_runs_status ON subagent_runs(status)
    WHERE status IN ('pending','running')""",
    "CREATE INDEX IF NOT EXISTS idx_usage_thread ON turn_usage(thread_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_usage_cost ON turn_usage(created_at)",
)


def sqlite_path_from_db_url(db_url: str | None = None) -> str:
    """Resolve sqlite database path suitable for aiosqlite.connect().

    Accepts sqlite:////absolute/path, sqlite:///relative/path, sqlite:///:memory:.
    If db_url is None, reads os.environ.get('DB_URL', DEFAULT_DB_URL).
    Raises ValueError if scheme is not sqlite or path is empty.
    """
    if db_url is None:
        db_url = os.environ.get("DB_URL", DEFAULT_DB_URL)
    stripped = db_url.strip()
    if not stripped:
        raise ValueError("Database URL is empty")
    prefix = "sqlite:///"
    if not stripped.lower().startswith(prefix):
        raise ValueError(f"Unsupported database URL: {db_url!r}")
    remainder = stripped[len(prefix) :]
    if not remainder:
        raise ValueError("SQLite URL path is empty")
    if remainder == ":memory:":
        return ":memory:"
    return remainder


async def configure_connection(conn: aiosqlite.Connection) -> None:
    """PRAGMA journal_mode=WAL; foreign_keys=ON; synchronous=NORMAL."""
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA synchronous=NORMAL")


async def apply_schema(conn: aiosqlite.Connection) -> None:
    """Create tables and migrate ``conversation_history`` when ``tool_name`` is missing."""
    for ddl in SCHEMA_DDLS:
        await conn.execute(ddl)
    await conn.commit()
    cursor = await conn.execute("PRAGMA table_info(conversation_history)")
    rows = await cursor.fetchall()
    await cursor.close()
    col_names = [r[1] for r in rows]
    if "tool_name" not in col_names:
        await conn.execute("ALTER TABLE conversation_history ADD COLUMN tool_name TEXT")
        await conn.commit()


async def open_connection(db_url: str | None = None) -> aiosqlite.Connection:
    """Open aiosqlite connection, configure WAL, return ready connection."""
    path = sqlite_path_from_db_url(db_url)
    conn = await aiosqlite.connect(path)
    await configure_connection(conn)
    return conn
