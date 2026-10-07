"""SQLite connection helpers and schema DDL for monkeybot persistence."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, TypeVar, cast

import aiosqlite

logger = logging.getLogger(__name__)

DEFAULT_DB_URL: Final[str] = "sqlite:///data/monkeybot.db"

OUTBOX_DDL: Final[str] = """CREATE TABLE IF NOT EXISTS memory_outbox (
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
)"""

OUTBOX_INDEX_DDL: Final[str] = (
    "CREATE INDEX IF NOT EXISTS idx_memory_outbox_pending "
    "ON memory_outbox(agent_id, palace_id, status, created_at)"
)

HISTORY_MESSAGE_ID_INDEX_DDL: Final[str] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_history_message_id "
    "ON conversation_history(message_id) WHERE message_id IS NOT NULL AND message_id != ''"
)


class TaskReentrantLock:
    """asyncio lock the owning task may re-enter (nested store methods)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if self._owner is task:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._depth = 1

    def release(self) -> None:
        task = asyncio.current_task()
        if self._owner is not task:
            raise RuntimeError("TaskReentrantLock.release() by non-owner")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self) -> TaskReentrantLock:
        await self.acquire()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.release()


F = TypeVar("F", bound=Callable[..., Any])
ConnLock = asyncio.Lock | TaskReentrantLock


def with_conn_lock(fn: F) -> F:
    """Serialize a store method on ``self._lock`` (re-entrant for nested calls)."""

    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            return await fn(self, *args, **kwargs)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__qualname__ = fn.__qualname__
    return cast(F, wrapper)


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

SESSION_BRANCHES_DDL: Final[str] = """CREATE TABLE IF NOT EXISTS session_branches (
    agent_scope TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    parent_branch_id TEXT,
    fork_row_id TEXT,
    op TEXT,
    created_at INTEGER NOT NULL,
    last_active_at INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent_scope, session_id, branch_id)
)"""

SESSION_BRANCHES_ACTIVE_INDEX_DDL: Final[str] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_session_branches_active "
    "ON session_branches(agent_scope, session_id) WHERE is_active = 1"
)

SCHEMA_DDLS: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS conversation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    agent_scope TEXT NOT NULL DEFAULT '',
    turn_id TEXT,
    message_id TEXT,
    row_id TEXT
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
    claimed_at_ms INTEGER,
    kind TEXT NOT NULL DEFAULT 'loop',
    objective TEXT,
    consecutive_error_count INTEGER NOT NULL DEFAULT 0
)""",
    """CREATE INDEX IF NOT EXISTS idx_scheduled_loops_due
    ON scheduled_loops(status, tick_in_flight, next_tick_at_ms)
    WHERE status = 'active'""",
    """CREATE TABLE IF NOT EXISTS session_turn_locks (
    session_id TEXT PRIMARY KEY,
    request_id TEXT,
    claimed_at_ms INTEGER
)""",
    """CREATE TABLE IF NOT EXISTS goal_ledger (
    entry_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    verbatim TEXT NOT NULL,
    provenance TEXT NOT NULL,
    channel TEXT,
    intent TEXT NOT NULL,
    status TEXT NOT NULL,
    relates_to TEXT,
    constraints_json TEXT NOT NULL,
    done_when_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    source_row_id TEXT
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_goal_ledger_thread_seq
    ON goal_ledger(thread_id, seq)""",
    # The active-branch index is created in ``_ensure_session_branches_shape``,
    # after a pre-row-id table has been rebuilt. It cannot live here: that
    # statement references ``agent_scope``, which the earlier table lacks.
    SESSION_BRANCHES_DDL,
    OUTBOX_DDL,
    OUTBOX_INDEX_DDL,
)


def sqlite_path_from_db_url(db_url: str | None = None) -> str:
    """Resolve sqlite database path suitable for aiosqlite.connect().

    Accepts sqlite:////absolute/path, sqlite:///relative/path, sqlite:///:memory:.
    If db_url is None, reads ``DB_URL`` from the runtime snapshot (default DEFAULT_DB_URL).
    Raises ValueError if scheme is not sqlite or path is empty.
    """
    if db_url is None:
        from monkeybot.core.config.snapshot import current_env

        db_url = current_env("DB_URL", DEFAULT_DB_URL)
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
    """PRAGMA journal_mode=WAL; foreign_keys=ON; synchronous=NORMAL; busy_timeout=5000."""
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA busy_timeout=5000")


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


async def apply_schema(conn: aiosqlite.Connection) -> None:
    """Create tables/indexes; refuse legacy conversation_history shapes."""
    for ddl in SCHEMA_DDLS:
        await conn.execute(ddl)
    await conn.commit()
    await _ensure_session_branches_shape(conn)
    await _ensure_turn_usage_estimated_column(conn)
    await _ensure_turn_usage_cache_columns(conn)
    await _ensure_subagent_runs_claim_columns(conn)
    await _ensure_conversation_history_agent_scope_column(conn)
    await _ensure_history_memory_columns(conn)
    await _ensure_history_row_id_column(conn)
    await _ensure_goal_ledger_source_row_column(conn)
    await _ensure_outbox_agent_id_column(conn)
    await _ensure_outbox_palace_id_column(conn)
    await _ensure_scheduled_loop_kind_columns(conn)
    cursor = await conn.execute("PRAGMA table_info(conversation_history)")
    rows = await cursor.fetchall()
    await cursor.close()
    col_names = {str(r[1]) for r in rows}
    if "tool_name" in col_names or "tool_call_id" in col_names:
        await _log_legacy_schema_error(conn)
        raise RuntimeError(_LEGACY_SCHEMA_MESSAGE)


async def _column_names(conn: aiosqlite.Connection, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    await cur.close()
    return {str(row[1]) for row in rows}


async def _ensure_session_branches_shape(conn: aiosqlite.Connection) -> None:
    """Rebuild a pre-row-id ``session_branches`` table, then index the active branch.

    The first branch schema stored ``fork_row_index`` and ``fork_fingerprint``
    and used ``PRIMARY KEY (session_id, branch_id)``. ``CREATE TABLE IF NOT
    EXISTS`` leaves that table in place, and SQLite cannot change its primary
    key with ``ALTER TABLE``. The active-branch index names ``agent_scope``,
    so it has to be created after the rebuild.
    """
    names = await _column_names(conn, "session_branches")
    if names and "agent_scope" not in names:
        await _rebuild_legacy_session_branches(conn)
    await conn.execute(SESSION_BRANCHES_ACTIVE_INDEX_DDL)
    await conn.commit()


async def _rebuild_legacy_session_branches(conn: aiosqlite.Connection) -> None:
    """Copy lineage into the current table. Old fork positions are not row ids."""
    logger.info(
        "rebuilding session_branches from the pre-row-id schema; "
        "fork_row_index and fork_fingerprint are dropped"
    )
    await conn.execute("BEGIN")
    try:
        await conn.execute("ALTER TABLE session_branches RENAME TO session_branches_legacy")
        await conn.execute(SESSION_BRANCHES_DDL)
        await conn.execute(
            """INSERT INTO session_branches (
                agent_scope, session_id, branch_id, thread_id, parent_branch_id,
                fork_row_id, op, created_at, last_active_at, is_active
            )
            SELECT
                '', session_id, branch_id, thread_id, parent_branch_id,
                NULL, op, created_at, last_active_at, is_active
            FROM session_branches_legacy"""
        )
        await conn.execute("DROP TABLE session_branches_legacy")
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise


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
        await conn.execute(f"ALTER TABLE turn_usage ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
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
    """Claim pre-migration rows (``agent_scope = ''``) for ``agent_scope``.

    Covers ``conversation_history`` and ``session_branches``. Rebuilt branch
    rows are inserted unscoped and stay invisible to a scoped store until this
    runs.

    Idempotent — the UPDATE simply matches zero rows once nothing is left
    unscoped, so callers should invoke this on every ``open()``, not just
    once after a schema change. Gating it on "did this call just add the
    column" instead is a bug: when another process's ``apply_schema()`` adds
    the column first (e.g. an unscoped worker sharing the same DB_URL), the
    scoped owner's own call sees the column already present and would never
    claim its legacy rows. Call only when :func:`db_owned_by_agent_root`
    confirms this SQLite file is uniquely owned by the agent about to claim
    it — see that function and ``docs/migrations/agent-scope-namespacing.md``
    for why unconditional auto-claiming (rejected in PR #179 review) is
    unsafe for a file that could be shared, but is correct here where
    ownership is unambiguous.
    """
    if not agent_scope:
        return
    await conn.execute(
        "UPDATE conversation_history SET agent_scope = ? WHERE agent_scope = ''",
        (agent_scope,),
    )
    # Rebuilt pre-row-id branch rows land with agent_scope '' and stay invisible
    # to a scoped store until claimed, same as legacy history.
    if "agent_scope" in await _column_names(conn, "session_branches"):
        await conn.execute(
            "UPDATE session_branches SET agent_scope = ? WHERE agent_scope = ''",
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


async def _ensure_outbox_agent_id_column(conn: aiosqlite.Connection) -> None:
    """Add agent_id on memory_outbox when upgrading an existing DB."""
    cur = await conn.execute("PRAGMA table_info(memory_outbox)")
    rows = await cur.fetchall()
    await cur.close()
    names = {str(r[1]) for r in rows}
    if not names:
        return
    if "agent_id" not in names:
        await conn.execute("ALTER TABLE memory_outbox ADD COLUMN agent_id TEXT NOT NULL DEFAULT ''")
    await conn.commit()


async def _ensure_history_memory_columns(conn: aiosqlite.Connection) -> None:
    """Add turn_id / message_id on conversation_history for the memory outbox."""
    cur = await conn.execute("PRAGMA table_info(conversation_history)")
    rows = await cur.fetchall()
    await cur.close()
    names = {str(r[1]) for r in rows}
    if not names:
        return
    if "turn_id" not in names:
        await conn.execute("ALTER TABLE conversation_history ADD COLUMN turn_id TEXT")
    if "message_id" not in names:
        await conn.execute("ALTER TABLE conversation_history ADD COLUMN message_id TEXT")
    await conn.execute(HISTORY_MESSAGE_ID_INDEX_DDL)
    await conn.commit()


async def _ensure_history_row_id_column(conn: aiosqlite.Connection) -> None:
    """Add ``row_id`` on conversation_history when upgrading an existing DB."""
    cur = await conn.execute("PRAGMA table_info(conversation_history)")
    rows = await cur.fetchall()
    await cur.close()
    names = {str(r[1]) for r in rows}
    if not names or "row_id" in names:
        return
    try:
        await conn.execute("ALTER TABLE conversation_history ADD COLUMN row_id TEXT")
        await conn.commit()
    except aiosqlite.OperationalError as exc:
        if "duplicate column name" not in str(exc):
            raise


async def _ensure_goal_ledger_source_row_column(conn: aiosqlite.Connection) -> None:
    """Add ``source_row_id`` on goal_ledger when upgrading an existing DB."""
    cur = await conn.execute("PRAGMA table_info(goal_ledger)")
    rows = await cur.fetchall()
    await cur.close()
    names = {str(r[1]) for r in rows}
    if not names or "source_row_id" in names:
        return
    try:
        await conn.execute("ALTER TABLE goal_ledger ADD COLUMN source_row_id TEXT")
        await conn.commit()
    except aiosqlite.OperationalError as exc:
        if "duplicate column name" not in str(exc):
            raise


async def _ensure_outbox_palace_id_column(conn: aiosqlite.Connection) -> None:
    """Add palace_id on memory_outbox when upgrading an existing DB."""
    cur = await conn.execute("PRAGMA table_info(memory_outbox)")
    rows = await cur.fetchall()
    await cur.close()
    names = {str(r[1]) for r in rows}
    if not names:
        return
    if "palace_id" not in names:
        await conn.execute(
            "ALTER TABLE memory_outbox ADD COLUMN palace_id TEXT NOT NULL DEFAULT ''"
        )
    await conn.execute(OUTBOX_INDEX_DDL)
    await conn.commit()


async def _ensure_scheduled_loop_kind_columns(conn: aiosqlite.Connection) -> None:
    """Migrate scheduled-loop kind fields and enforce one open goal per session."""
    cur = await conn.execute("PRAGMA table_info(scheduled_loops)")
    rows = await cur.fetchall()
    await cur.close()
    names = {str(r[1]) for r in rows}
    if not names:
        return
    if "kind" not in names:
        await conn.execute(
            "ALTER TABLE scheduled_loops ADD COLUMN kind TEXT NOT NULL DEFAULT 'loop'"
        )
    if "objective" not in names:
        await conn.execute("ALTER TABLE scheduled_loops ADD COLUMN objective TEXT")
    if "consecutive_error_count" not in names:
        await conn.execute(
            "ALTER TABLE scheduled_loops "
            "ADD COLUMN consecutive_error_count INTEGER NOT NULL DEFAULT 0"
        )
    await conn.execute(
        """
        UPDATE scheduled_loops
        SET status = 'completed',
            stop_reason = COALESCE(stop_reason, 'superseded_migration'),
            tick_in_flight = 0,
            worker_id = NULL,
            claimed_at_ms = NULL
        WHERE loop_id IN (
            SELECT loop_id
            FROM (
                SELECT loop_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY session_id
                           ORDER BY started_at_ms DESC, loop_id DESC
                       ) AS open_rank
                FROM scheduled_loops
                WHERE kind = 'goal' AND status IN ('active', 'paused')
            )
            WHERE open_rank > 1
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scheduled_loops_kind_session "
        "ON scheduled_loops(kind, session_id, status)"
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_loops_one_open_goal "
        "ON scheduled_loops(session_id) "
        "WHERE kind = 'goal' AND status IN ('active', 'paused')"
    )
    await conn.commit()


async def open_connection(db_url: str | None = None) -> aiosqlite.Connection:
    """Open aiosqlite connection, configure WAL, return ready connection.

    ``isolation_level=None`` (autocommit) so manual ``BEGIN IMMEDIATE`` in
    ``append_with_outbox`` / ``reset`` cannot nest inside an implicit
    sqlite3 transaction. Same reason as ``observability/sqlite_exporter``.
    """
    path = sqlite_path_from_db_url(db_url)
    if path != ":memory:":
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path, isolation_level=None)
    await configure_connection(conn)
    return conn
