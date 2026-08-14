"""SQLite connection helpers and schema DDL for monkeybot persistence."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final

import aiosqlite

logger = logging.getLogger(__name__)

DEFAULT_DB_URL: Final[str] = "sqlite:///data/monkeybot.db"

_LEGACY_SCHEMA_MESSAGE = """Legacy conversation_history schema detected (tool_name and/or
tool_call_id columns present). The on-disk format changed in the
typed-message-content release. Choose one:

  Local SQLite (dev / smoke agent):
    rm -f data/monkeybot.db
    rm -f data/monkeybot.db-shm
    rm -f data/monkeybot.db-wal
  (or the same files under your configured workspace data dir)

  Other deployments: there is no automated migration. The legacy format is
  a JSON tail inside a TEXT column; transcribing it to typed blocks must
  be done by the operator. See docs/migrations/typed-message-content.md."""

SCHEMA_DDLS: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS conversation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL,
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
    started_at INTEGER,
    finished_at INTEGER,
    scratch_dir TEXT,
    worker_id TEXT,
    claimed_at INTEGER
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
    context_json TEXT,
    estimated_prompt_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0
)""",
    "CREATE INDEX IF NOT EXISTS idx_history_thread ON conversation_history(thread_id, created_at)",
    """CREATE INDEX IF NOT EXISTS idx_history_thread_last
    ON conversation_history(thread_id, created_at DESC, id DESC)""",
    "CREATE INDEX IF NOT EXISTS idx_runs_parent ON subagent_runs(parent_run_id)",
    """CREATE INDEX IF NOT EXISTS idx_runs_status ON subagent_runs(status)
    WHERE status IN ('pending','running')""",
    "CREATE INDEX IF NOT EXISTS idx_usage_thread ON turn_usage(thread_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_usage_cost ON turn_usage(created_at)",
    """CREATE TABLE IF NOT EXISTS scheduled_loops (
    loop_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    prompt TEXT NOT NULL,
    interval_ms INTEGER NOT NULL,
    max_ticks INTEGER,
    max_runtime_ms INTEGER,
    skip_if_busy INTEGER NOT NULL DEFAULT 1,
    tick_index INTEGER NOT NULL DEFAULT 0,
    next_tick_at_ms INTEGER NOT NULL,
    started_at_ms INTEGER NOT NULL,
    last_tick_at_ms INTEGER,
    last_error TEXT,
    stop_reason TEXT,
    tick_in_flight INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    claimed_at_ms INTEGER
)""",
    """CREATE INDEX IF NOT EXISTS idx_scheduled_loops_due
    ON scheduled_loops(status, tick_in_flight, next_tick_at_ms)
    WHERE status = 'active'""",
    """CREATE TABLE IF NOT EXISTS session_turn_locks (
    session_id TEXT PRIMARY KEY,
    request_id TEXT,
    claimed_at_ms INTEGER
)""",
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


def db_owned_by_agent_root(db_url: str, agent_root: Path | None) -> bool:
    """True when ``db_url`` resolves to a file this ``agent_root`` uniquely owns.

    The default deployment (``paths.db_url: sqlite:///data/monkeybot.db``,
    left relative) is anchored under the agent root by
    :func:`~monkeybot.core.layout.resolve_sqlite_url`, so by construction no
    other agent would sensibly point there too — safe to auto-claim legacy
    rows for on migration (see :func:`backfill_legacy_agent_scope`). An
    operator-set *absolute* path is not anchored that way and may be
    deliberately shared across agents (the case PR #179 review reproduced a
    real cross-agent leak against) — never safe to auto-claim.
    """
    if agent_root is None:
        return False
    try:
        path = sqlite_path_from_db_url(db_url)
    except ValueError:
        return False
    if path == ":memory:":
        return True  # ephemeral, in-process only — inherently single-owner
    try:
        return Path(path).resolve().is_relative_to(agent_root.resolve())
    except (OSError, ValueError):
        return False


async def configure_connection(conn: aiosqlite.Connection) -> None:
    """PRAGMA journal_mode=WAL; foreign_keys=ON; synchronous=NORMAL."""
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA synchronous=NORMAL")


async def _connection_db_path(conn: aiosqlite.Connection) -> str:
    """Resolve main DB path for logging (``:memory:`` when no file path)."""
    cursor = await conn.execute("PRAGMA database_list")
    rows = await cursor.fetchall()
    await cursor.close()
    for row in rows:
        if len(row) >= 3 and str(row[1]) == "main":
            file_col = row[2]
            return ":memory:" if not file_col else str(file_col)
    return ":memory:"


async def _log_legacy_schema_error(conn: aiosqlite.Connection) -> None:
    path = await _connection_db_path(conn)
    logger.error("Legacy conversation_history schema detected; db=%s", path)


async def apply_schema(conn: aiosqlite.Connection) -> bool:
    """Create tables/indexes; refuse legacy conversation_history shapes.

    Returns True when ``agent_scope`` was just added to a pre-existing
    ``conversation_history`` table — a fresh migration off the pre-agent-scope
    schema. Callers use this to decide whether to claim legacy rows for the
    scope they're about to open with (see :func:`backfill_legacy_agent_scope`).
    """
    for ddl in SCHEMA_DDLS:
        await conn.execute(ddl)
    await conn.commit()
    await _ensure_turn_usage_estimated_column(conn)
    await _ensure_turn_usage_cache_columns(conn)
    await _ensure_subagent_runs_claim_columns(conn)
    agent_scope_migrated = await _ensure_conversation_history_agent_scope_column(conn)
    cursor = await conn.execute("PRAGMA table_info(conversation_history)")
    rows = await cursor.fetchall()
    await cursor.close()
    col_names = {str(r[1]) for r in rows}
    if "tool_name" in col_names or "tool_call_id" in col_names:
        await _log_legacy_schema_error(conn)
        raise RuntimeError(_LEGACY_SCHEMA_MESSAGE)
    return agent_scope_migrated


async def _ensure_turn_usage_estimated_column(conn: aiosqlite.Connection) -> None:
    """Add ``estimated_prompt_tokens`` when upgrading an existing DB."""
    cur = await conn.execute("PRAGMA table_info(turn_usage)")
    rows = await cur.fetchall()
    await cur.close()
    names = {str(r[1]) for r in rows}
    if "estimated_prompt_tokens" in names:
        return
    await conn.execute(
        "ALTER TABLE turn_usage ADD COLUMN estimated_prompt_tokens INTEGER NOT NULL DEFAULT 0"
    )
    await conn.commit()


async def _ensure_turn_usage_cache_columns(conn: aiosqlite.Connection) -> None:
    """Add cache token columns when upgrading an existing DB."""
    cur = await conn.execute("PRAGMA table_info(turn_usage)")
    rows = await cur.fetchall()
    await cur.close()
    names = {str(r[1]) for r in rows}
    for col in ("cache_read_tokens", "cache_creation_tokens"):
        if col in names:
            continue
        await conn.execute(
            f"ALTER TABLE turn_usage ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"
        )
    await conn.commit()


async def _ensure_subagent_runs_claim_columns(conn: aiosqlite.Connection) -> None:
    """Add worker claim columns when upgrading an existing DB."""
    cur = await conn.execute("PRAGMA table_info(subagent_runs)")
    rows = await cur.fetchall()
    await cur.close()
    names = {str(r[1]) for r in rows}
    if "worker_id" not in names:
        await conn.execute("ALTER TABLE subagent_runs ADD COLUMN worker_id TEXT")
    if "claimed_at" not in names:
        await conn.execute("ALTER TABLE subagent_runs ADD COLUMN claimed_at INTEGER")
    await conn.commit()


async def _ensure_conversation_history_agent_scope_column(conn: aiosqlite.Connection) -> bool:
    """Add ``agent_scope`` when upgrading an existing DB. Returns True if it was just added.

    The scoped index is created here, after the column exists, rather than in
    ``SCHEMA_DDLS``, which runs first and would fail referencing a column not
    yet on a pre-existing table.
    """
    cur = await conn.execute("PRAGMA table_info(conversation_history)")
    rows = await cur.fetchall()
    await cur.close()
    names = {str(r[1]) for r in rows}
    column_added = "agent_scope" not in names
    if column_added:
        try:
            await conn.execute(
                "ALTER TABLE conversation_history ADD COLUMN agent_scope TEXT NOT NULL DEFAULT ''"
            )
            await conn.commit()
        except aiosqlite.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise
            # Two gateways opened this same shared file concurrently and both
            # saw the column missing; the other one's ALTER already committed
            # between our check and this statement — fine, it exists now.
    await conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_history_scope_thread
        ON conversation_history(agent_scope, thread_id, created_at DESC, id DESC)"""
    )
    await conn.commit()
    return column_added


async def backfill_legacy_agent_scope(conn: aiosqlite.Connection, agent_scope: str) -> None:
    """Claim pre-migration rows (``agent_scope = ''``) for ``agent_scope``, once.

    Call only when :func:`apply_schema` reports a fresh migration AND
    :func:`db_owned_by_agent_root` confirms this SQLite file is uniquely
    owned by the agent about to claim it — see that function and
    ``docs/migrations/agent-scope-namespacing.md`` for why unconditional
    auto-claiming (rejected in PR #179 review) is unsafe for a file that
    could be shared, but is correct here where ownership is unambiguous.
    """
    if not agent_scope:
        return
    await conn.execute(
        "UPDATE conversation_history SET agent_scope = ? WHERE agent_scope = ''",
        (agent_scope,),
    )
    await conn.commit()


async def warn_if_legacy_unscoped_history(conn: aiosqlite.Connection) -> None:
    """Log once if pre-migration (``agent_scope = ''``) rows remain.

    Unreachable via ``list_threads``/``load``/``reset`` from any agent-scoped
    store until an operator runs, per thread_id (see
    ``docs/migrations/agent-scope-namespacing.md``)::

        UPDATE conversation_history SET agent_scope = '<agent-id>'
        WHERE thread_id = '<thread-id>' AND agent_scope = '';
    """
    cur = await conn.execute(
        "SELECT EXISTS(SELECT 1 FROM conversation_history WHERE agent_scope = '')"
    )
    row = await cur.fetchone()
    await cur.close()
    if row and row[0]:
        logger.warning(
            "conversation_history has rows with agent_scope='' that this agent "
            "did not write — they are not reachable via list_threads, load, or "
            "reset until an operator backfills agent_scope for each legacy "
            "thread_id by hand (see warn_if_legacy_unscoped_history docstring "
            "for the exact UPDATE statement)."
        )


async def open_connection(db_url: str | None = None) -> aiosqlite.Connection:
    """Open aiosqlite connection, configure WAL, return ready connection."""
    path = sqlite_path_from_db_url(db_url)
    if path != ":memory:":
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    await configure_connection(conn)
    return conn
