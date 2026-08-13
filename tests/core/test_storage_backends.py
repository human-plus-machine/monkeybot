"""Tests for StorageBackend protocol implementations and the create_storage_backend factory."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio

from monkeybot.core.llm.provider import Message
from monkeybot.core.llm.usage import Usage, UsageSummary
from monkeybot.core.persistence.backends import create_storage_backend
from monkeybot.core.persistence.durable_runs import SubagentEnvelope, SubagentRunRow
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sqlite_backend():
    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    yield backend
    await backend.close()


def _make_envelope(task: str = "do something", parent_run_id: str = "p1") -> SubagentEnvelope:
    return SubagentEnvelope(
        task=task,
        context="ctx",
        memory_storage_uri="local:///mem",
        parent_run_id=parent_run_id,
    )


# ---------------------------------------------------------------------------
# Auto schema (paths.auto_schema)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_open_run_schema_false_skips_apply_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    async def spy_apply(conn: object) -> None:
        calls.append(conn)

    monkeypatch.setattr(
        "monkeybot.core.persistence.sqlite_backend.apply_schema",
        spy_apply,
    )
    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open(run_schema=False)
    assert calls == []
    await backend.close()


@pytest.mark.asyncio
async def test_sqlite_open_run_schema_false_reads_writes_after_manual_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from monkeybot.core.persistence.sqlite import apply_schema

    calls: list[object] = []

    async def spy_apply(conn: object) -> None:
        calls.append(conn)

    monkeypatch.setattr(
        "monkeybot.core.persistence.sqlite_backend.apply_schema",
        spy_apply,
    )
    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open(run_schema=False)
    assert calls == []
    await apply_schema(backend._conn)  # type: ignore[arg-type]
    store = backend.history()
    msg = Message.text("user", "hello")
    await store.append("t-auto-schema", msg)
    assert await store.load("t-auto-schema") == [msg]
    await backend.close()


@pytest.mark.asyncio
async def test_postgres_open_run_schema_false_skips_apply_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        pytest.skip("asyncpg is not installed")

    from unittest.mock import AsyncMock, MagicMock

    from monkeybot.core.persistence.postgres import PostgresStorageBackend

    apply_calls: list[object] = []

    async def spy_apply(pool: object) -> None:
        apply_calls.append(pool)

    mock_pool = MagicMock()
    mock_pool.close = AsyncMock()
    monkeypatch.setattr(
        "monkeybot.core.persistence.postgres.asyncpg.create_pool",
        AsyncMock(return_value=mock_pool),
    )
    monkeypatch.setattr(
        "monkeybot.core.persistence.postgres._apply_schema",
        spy_apply,
    )

    backend = PostgresStorageBackend("postgresql://localhost/test")
    await backend.open(run_schema=False)
    assert apply_calls == []
    assert backend._pool is mock_pool
    loops = backend.scheduled_loops()
    assert loops is backend.scheduled_loops()
    await backend.close()


def _require_asyncpg() -> None:
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        pytest.skip("asyncpg is not installed")


class _FakeAsyncpgConn:
    """Records every SQL call and validates ``$N`` placeholder count vs. args passed.

    A pure string-capture mock (recording the query but never checking arity)
    would not have caught a real bug found while validating this fix against a
    live Postgres: reset() passed 3 positional args against a 2-placeholder
    query, which asyncpg rejects with "the server expects N arguments...".
    Checking placeholder count here catches that class of bug without a live DB.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def _check_arity(self, query: str, args: tuple[object, ...]) -> None:
        import re

        placeholders = {int(m) for m in re.findall(r"\$(\d+)", query)}
        expected = max(placeholders) if placeholders else 0
        assert len(args) == expected, (
            f"query expects {expected} args (placeholders {sorted(placeholders)}), "
            f"got {len(args)}: {query!r}"
        )

    async def fetch(self, query: str, *args: object) -> list[object]:
        self._check_arity(query, args)
        self.calls.append((query, args))
        return []

    async def fetchval(self, query: str, *args: object) -> object:
        self._check_arity(query, args)
        self.calls.append((query, args))
        return None

    async def execute(self, query: str, *args: object) -> str:
        self._check_arity(query, args)
        self.calls.append((query, args))
        return "OK"

    def transaction(self) -> _FakeAsyncpgTransaction:
        return _FakeAsyncpgTransaction()


class _FakeAsyncpgTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeAsyncpgAcquire:
    def __init__(self, conn: _FakeAsyncpgConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeAsyncpgConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeAsyncpgPool:
    def __init__(self, conn: _FakeAsyncpgConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAsyncpgAcquire:
        return _FakeAsyncpgAcquire(self._conn)


@pytest.mark.asyncio
async def test_postgres_list_threads_query_does_not_reference_ungrouped_column() -> None:
    """Regression for PR #179 review: PostgreSQL rejects a subquery that reads
    an outer-query column (``h.agent_scope``) not present in GROUP BY, with
    "subquery uses ungrouped column" — verified against a real local Postgres
    instance. The scoped subquery must bind the scope as a query parameter
    (``$1``) instead of correlating through ``h.agent_scope``.
    """
    _require_asyncpg()
    from monkeybot.core.persistence.postgres import PostgresHistoryStore

    fake_conn = _FakeAsyncpgConn()
    store = PostgresHistoryStore(_FakeAsyncpgPool(fake_conn), "agent-a")  # type: ignore[arg-type]
    await store.list_threads()

    assert len(fake_conn.calls) == 1
    query = fake_conn.calls[0][0]
    assert "h2.agent_scope = h.agent_scope" not in query


@pytest.mark.asyncio
async def test_postgres_load_reset_clear_use_correct_arity() -> None:
    """Regression: reset()/clear()/load() must pass exactly as many args as
    their query has ``$N`` placeholders for — verified live against a real
    Postgres instance (see test_storage_backends manual validation in the
    PR #179 review response); this mock catches the same class of bug
    without needing a live DB in CI.
    """
    _require_asyncpg()
    from monkeybot.core.persistence.postgres import PostgresHistoryStore

    fake_conn = _FakeAsyncpgConn()
    store = PostgresHistoryStore(_FakeAsyncpgPool(fake_conn), "agent-a")  # type: ignore[arg-type]

    await store.load("t1")
    await store.load("t1", limit=5)
    await store.clear("t1")
    await store.reset("t1", [])

    assert len(fake_conn.calls) == 4


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


def test_factory_returns_sqlite_backend_for_sqlite_url() -> None:
    backend = create_storage_backend("sqlite:///:memory:")
    assert isinstance(backend, SQLiteStorageBackend)


def test_factory_raises_value_error_for_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="Unsupported DB URL scheme"):
        create_storage_backend("mysql://localhost/db")


def test_factory_raises_runtime_error_for_postgres_without_asyncpg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Setting a module to ``None`` in sys.modules forces the next import of it
    # to raise ImportError, regardless of whether asyncpg is actually
    # installed in this environment — deterministic across all CI configs.
    monkeypatch.setitem(sys.modules, "monkeybot.core.persistence.postgres", None)

    with pytest.raises(RuntimeError, match="asyncpg is not installed"):
        create_storage_backend("postgresql://localhost/db")


def test_factory_postgres_scheme_alias_without_asyncpg(monkeypatch: pytest.MonkeyPatch) -> None:
    """``postgres://`` must hit the Postgres branch (not ValueError), same as postgresql://."""
    monkeypatch.setitem(sys.modules, "monkeybot.core.persistence.postgres", None)

    with pytest.raises(RuntimeError, match="asyncpg is not installed"):
        create_storage_backend("postgres://localhost/db")


def test_db_compat_module_reexports_sqlite() -> None:
    import monkeybot.core.persistence.db as db_mod
    import monkeybot.core.persistence.sqlite as sqlite_mod

    assert db_mod.open_connection is sqlite_mod.open_connection
    assert db_mod.apply_schema is sqlite_mod.apply_schema
    assert db_mod.sqlite_path_from_db_url is sqlite_mod.sqlite_path_from_db_url


def test_importing_backends_does_not_load_sqlite_impl() -> None:
    """backends.py must not load any impl modules at import time.

    After importing backends (which is already imported), neither sqlite_backend
    nor aiosqlite's internal modules should appear as a side-effect of just
    importing backends alone. We verify by checking that sqlite_backend was NOT
    in sys.modules before the first factory call in this test session — the
    module is only loaded when create_storage_backend("sqlite://...") is called.

    Since other tests may have already triggered the lazy import, we do a
    structural check instead: confirm that backends.py has no top-level
    import of sqlite_backend by inspecting the module's __dict__.
    """
    import monkeybot.core.persistence.backends as backends_mod

    # The module dict should not contain SQLiteStorageBackend at the top level.
    assert "SQLiteStorageBackend" not in backends_mod.__dict__, (
        "backends.py should not import SQLiteStorageBackend at module level"
    )
    # PostgresStorageBackend should also not be top-level.
    assert "PostgresStorageBackend" not in backends_mod.__dict__, (
        "backends.py should not import PostgresStorageBackend at module level"
    )


# ---------------------------------------------------------------------------
# SQLiteStorageBackend — open/close lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_backend_raises_before_open() -> None:
    backend = SQLiteStorageBackend("sqlite:///:memory:")
    with pytest.raises(RuntimeError, match="open\\(\\) has not been called"):
        backend.history()
    with pytest.raises(RuntimeError, match="open\\(\\) has not been called"):
        backend.usage()
    with pytest.raises(RuntimeError, match="open\\(\\) has not been called"):
        backend.runs()
    with pytest.raises(RuntimeError, match="open\\(\\) has not been called"):
        backend.scheduled_loops()


@pytest.mark.asyncio
async def test_sqlite_backend_history_returns_same_instance(sqlite_backend: SQLiteStorageBackend) -> None:
    h1 = sqlite_backend.history()
    h2 = sqlite_backend.history()
    assert h1 is h2


@pytest.mark.asyncio
async def test_sqlite_backend_usage_returns_same_instance(sqlite_backend: SQLiteStorageBackend) -> None:
    u1 = sqlite_backend.usage()
    u2 = sqlite_backend.usage()
    assert u1 is u2


@pytest.mark.asyncio
async def test_sqlite_backend_runs_returns_same_instance(sqlite_backend: SQLiteStorageBackend) -> None:
    r1 = sqlite_backend.runs()
    r2 = sqlite_backend.runs()
    assert r1 is r2


@pytest.mark.asyncio
async def test_sqlite_backend_scheduled_loops_returns_same_instance(
    sqlite_backend: SQLiteStorageBackend,
) -> None:
    s1 = sqlite_backend.scheduled_loops()
    s2 = sqlite_backend.scheduled_loops()
    assert s1 is s2


# ---------------------------------------------------------------------------
# HistoryStore protocol — append / load / reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_append_and_load_roundtrip(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.history()
    msg = Message.text("user", "hello")
    await store.append("t1", msg)
    loaded = await store.load("t1")
    assert loaded == [msg]


@pytest.mark.asyncio
async def test_history_load_returns_oldest_first(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.history()
    msgs = [Message.text("user", f"m{i}") for i in range(3)]
    for m in msgs:
        await store.append("t2", m)
    loaded = await store.load("t2")
    assert loaded == msgs


@pytest.mark.asyncio
async def test_history_load_respects_limit(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.history()
    for i in range(10):
        await store.append("t3", Message.text("user", f"m{i}"))
    loaded = await store.load("t3", limit=5)
    assert len(loaded) == 5


@pytest.mark.asyncio
async def test_history_reset_replaces_transcript(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.history()
    for i in range(5):
        await store.append("t4", Message.text("user", f"old{i}"))
    new_msgs = [Message.text("user", "a"), Message.text("assistant", "b")]
    await store.reset("t4", new_msgs)
    loaded = await store.load("t4")
    assert loaded == new_msgs


@pytest.mark.asyncio
async def test_history_reset_to_empty(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.history()
    await store.append("t5", Message.text("user", "x"))
    await store.reset("t5", [])
    assert await store.load("t5") == []


@pytest.mark.asyncio
async def test_history_load_empty_thread_returns_empty(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.history()
    assert await store.load("nonexistent") == []


# ---------------------------------------------------------------------------
# agent_scope isolation — different agent roots sharing one DB_URL must not
# see each other's threads (PR #179 review: chat.py:703).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_list_threads_isolated_by_agent_scope() -> None:
    backend_a = SQLiteStorageBackend("sqlite:///:memory:", agent_scope="agent-a")
    await backend_a.open()
    try:
        conn = backend_a._conn
        assert conn is not None
        # Share the same connection/db to simulate one DB_URL, two agent roots.
        from monkeybot.core.persistence.history import SQLiteHistoryStore

        store_a = backend_a.history()
        store_b = SQLiteHistoryStore(conn, "agent-b")

        await store_a.append("t1", Message.text("user", "agent a's secret"))
        await store_b.append("t2", Message.text("user", "agent b's secret"))

        threads_a = await store_a.list_threads()
        threads_b = await store_b.list_threads()

        assert [t.thread_id for t in threads_a] == ["t1"]
        assert [t.thread_id for t in threads_b] == ["t2"]
        # Neither agent can load the other's thread by id either.
        assert await store_a.load("t2") == []
        assert await store_b.load("t1") == []
    finally:
        await backend_a.close()


@pytest.mark.asyncio
async def test_history_list_threads_excludes_subagent_transcripts() -> None:
    """Regression for PR #179 review: a subagent that finishes after its
    parent's last turn must not outrank the parent as "newest" — otherwise
    `monkeybot chat --continue` resumes the subagent's transcript under the
    main-agent prompt and tools instead of the actual previous chat.
    """
    from monkeybot.core.persistence.thread_summary import SUBAGENT_THREAD_ID_PREFIX

    backend = SQLiteStorageBackend("sqlite:///:memory:", agent_scope="agent-a")
    await backend.open()
    try:
        store = backend.history()
        await store.append("main-thread", Message.text("user", "hello"))
        # Subagent finishes strictly after the parent's last message.
        await store.append(
            f"{SUBAGENT_THREAD_ID_PREFIX}main-thread:abc123",
            Message.text("user", "subagent internal chatter"),
        )

        threads = await store.list_threads()
        assert [t.thread_id for t in threads] == ["main-thread"]
        # The subagent's own transcript is still fully readable by its exact id —
        # only list_threads (auto-discovery / --continue) excludes it.
        loaded = await store.load(f"{SUBAGENT_THREAD_ID_PREFIX}main-thread:abc123")
        assert len(loaded) == 1
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_history_reset_does_not_cross_scope(tmp_path: Path) -> None:
    from monkeybot.core.persistence.history import SQLiteHistoryStore

    db_path = tmp_path / "shared.db"
    backend_a = SQLiteStorageBackend(f"sqlite:///{db_path}", agent_scope="agent-a")
    await backend_a.open()
    try:
        conn = backend_a._conn
        assert conn is not None
        store_a = backend_a.history()
        store_b = SQLiteHistoryStore(conn, "agent-b")

        await store_a.append("same-id", Message.text("user", "a's message"))
        await store_b.append("same-id", Message.text("user", "b's message"))

        await store_a.reset("same-id", [])

        assert await store_a.load("same-id") == []
        assert await store_b.load("same-id") == [Message.text("user", "b's message")]
    finally:
        await backend_a.close()


@pytest.mark.asyncio
async def test_history_migration_does_not_auto_claim_legacy_rows(tmp_path: Path) -> None:
    """Regression for PR #179 review: an earlier version of this fix claimed
    every pre-migration (agent_scope='') row for whichever agent opened the DB
    first. Reproduced directly here with two agents' legacy threads on one
    file: that let the first opener read the second agent's secret and left
    the second agent's own history empty. Migration must leave ambiguous
    legacy rows unclaimed by anyone — only an explicit, per-thread operator
    UPDATE (exercised below) may assign them.
    """
    import time

    import aiosqlite

    db_path = tmp_path / "legacy.db"
    conn = await aiosqlite.connect(str(db_path))
    await conn.execute(
        """CREATE TABLE conversation_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )"""
    )
    await conn.execute(
        "INSERT INTO conversation_history(thread_id, role, content, created_at) VALUES (?,?,?,?)",
        ("agent-a-legacy-thread", "user", '[{"type":"text","text":"a\'s secret"}]', int(time.time() * 1000)),
    )
    await conn.execute(
        "INSERT INTO conversation_history(thread_id, role, content, created_at) VALUES (?,?,?,?)",
        ("agent-b-legacy-thread", "user", '[{"type":"text","text":"b\'s secret"}]', int(time.time() * 1000)),
    )
    await conn.commit()
    await conn.close()

    backend_a = SQLiteStorageBackend(f"sqlite:///{db_path}", agent_scope="agent-a")
    await backend_a.open(run_schema=True)
    try:
        # Migration must not have claimed either thread for agent-a.
        assert await backend_a.history().list_threads() == []
        assert await backend_a.history().load("agent-a-legacy-thread") == []
        assert await backend_a.history().load("agent-b-legacy-thread") == []

        # The documented manual path (see warn_if_legacy_unscoped_history) does
        # restore access, scoped to exactly the thread an operator maps.
        conn2 = backend_a._conn
        assert conn2 is not None
        await conn2.execute(
            "UPDATE conversation_history SET agent_scope = ? WHERE thread_id = ? AND agent_scope = ''",
            ("agent-a", "agent-a-legacy-thread"),
        )
        await conn2.commit()
        assert [t.thread_id for t in await backend_a.history().list_threads()] == [
            "agent-a-legacy-thread"
        ]
        # agent-b's still-unclaimed thread remains invisible to agent-a.
        assert await backend_a.history().load("agent-b-legacy-thread") == []
    finally:
        await backend_a.close()


@pytest.mark.asyncio
async def test_history_warns_once_when_legacy_unscoped_rows_remain(tmp_path: Path) -> None:
    from unittest.mock import patch

    from monkeybot.core.persistence.sqlite import warn_if_legacy_unscoped_history

    db_path = tmp_path / "legacy2.db"
    backend = SQLiteStorageBackend(f"sqlite:///{db_path}", agent_scope="agent-a")
    await backend.open(run_schema=True)
    try:
        conn = backend._conn
        assert conn is not None
        await conn.execute(
            "INSERT INTO conversation_history(thread_id, role, content, created_at, agent_scope) "
            "VALUES (?,?,?,?,?)",
            ("stray-thread", "user", '[{"type":"text","text":"x"}]', 1, ""),
        )
        await conn.commit()
        with patch("monkeybot.core.persistence.sqlite.logger") as mock_logger:
            await warn_if_legacy_unscoped_history(conn)
            mock_logger.warning.assert_called_once()
    finally:
        await backend.close()


# ---------------------------------------------------------------------------
# UsageStore protocol — record / summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_record_and_summary_roundtrip(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.usage()
    u = Usage(input_tokens=10, output_tokens=5, cached_tokens=1, cost_usd=0.001, duration_ms=100)
    await store.record("sess1", "gemini-2.5-flash", u, run_id="run-1")
    summary = await store.summary(thread_id="sess1")
    assert isinstance(summary, UsageSummary)
    assert summary.turns == 1
    assert summary.input_tokens == 10
    assert summary.output_tokens == 5
    assert summary.cost_usd == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_usage_summary_aggregates_multiple_turns(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.usage()
    for cost in [0.1, 0.2, 0.3]:
        u = Usage(input_tokens=1, output_tokens=1, cost_usd=cost, duration_ms=1)
        await store.record("sess2", "gemini", u)
    summary = await store.summary(thread_id="sess2")
    assert summary.turns == 3
    assert summary.cost_usd == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_usage_summary_empty_returns_zeros(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.usage()
    summary = await store.summary(thread_id="no-such-thread")
    assert summary.turns == 0
    assert summary.cost_usd == 0.0


@pytest.mark.asyncio
async def test_usage_summary_since_ms_filters_old_rows(sqlite_backend: SQLiteStorageBackend) -> None:
    """since_ms filtering exercised via direct SQL inserts to control created_at."""
    import aiosqlite

    conn: aiosqlite.Connection = sqlite_backend._conn  # type: ignore[assignment]
    for created_at, cost in [(500, 0.1), (1500, 0.2)]:
        await conn.execute(
            """
            INSERT INTO turn_usage(
                thread_id, run_id, model,
                input_tokens, output_tokens, cached_tokens,
                cost_usd, duration_ms, created_at, context_json,
                estimated_prompt_tokens
            )
            VALUES (?, NULL, 'gemini', 1, 1, 0, ?, 1, ?, NULL, 0)
            """,
            ("sess3", cost, created_at),
        )
    await conn.commit()

    store = sqlite_backend.usage()
    summary = await store.summary(thread_id="sess3", since_ms=1000)
    assert summary.turns == 1
    assert summary.cost_usd == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# RunStore protocol — record_started / completed / failed / pending / get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_record_started_get_run_roundtrip(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.runs()
    env = _make_envelope(task="write tests", parent_run_id="parent-1")
    await store.record_started(
        run_id="run-001",
        parent_run_id="parent-1",
        script="subagents/writer.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-001"),
    )
    row = await store.get_run("run-001")
    assert row is not None
    assert isinstance(row, SubagentRunRow)
    assert row.run_id == "run-001"
    assert row.status == "running"
    assert row.parent_run_id == "parent-1"
    assert row.script == "subagents/writer.py"
    assert row.scratch_dir == "/tmp/run-001"
    assert row.envelope_json == env.to_json()


@pytest.mark.asyncio
async def test_run_record_started_not_in_pending_runs(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="p2")
    await store.record_started(
        run_id="run-002",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-002"),
    )
    pending = await store.pending_runs()
    run_ids = [r.run_id for r in pending]
    assert "run-002" not in run_ids


@pytest.mark.asyncio
async def test_run_record_completed_removes_from_pending(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="p3")
    await store.record_pending(
        run_id="run-003",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-003"),
    )
    await store.record_completed("run-003", '{"ok": true}')
    pending = await store.pending_runs()
    assert all(r.run_id != "run-003" for r in pending)


@pytest.mark.asyncio
async def test_run_record_completed_updates_status(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="p4")
    await store.record_started(
        run_id="run-004",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-004"),
    )
    await store.record_completed("run-004", '{"result": "done"}')
    row = await store.get_run("run-004")
    assert row is not None
    assert row.status == "completed"
    assert row.result_json == '{"result": "done"}'
    assert row.finished_at is not None


@pytest.mark.asyncio
async def test_run_record_failed_sets_status_and_error(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="p5")
    await store.record_started(
        run_id="run-005",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-005"),
    )
    await store.record_failed("run-005", "something exploded")
    row = await store.get_run("run-005")
    assert row is not None
    assert row.status == "failed"
    assert row.error_json is not None
    assert "something exploded" in row.error_json
    assert row.finished_at is not None


@pytest.mark.asyncio
async def test_run_get_run_returns_none_for_missing(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.runs()
    assert await store.get_run("no-such-run") is None


@pytest.mark.asyncio
async def test_run_pending_runs_excludes_failed(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="p6")
    await store.record_started(
        run_id="run-006",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-006"),
    )
    await store.record_failed("run-006", "boom")
    pending = await store.pending_runs()
    assert all(r.run_id != "run-006" for r in pending)


@pytest.mark.asyncio
async def test_run_pending_runs_orders_by_started_at(sqlite_backend: SQLiteStorageBackend) -> None:
    """pending_runs returns rows oldest-first."""
    store = sqlite_backend.runs()
    for i in range(3):
        env = _make_envelope(parent_run_id=f"p-ord-{i}")
        await store.record_pending(
            run_id=f"run-ord-{i}",
            parent_run_id=None,
            script="subagents/x.py",
            envelope=env,
            scratch_dir=Path(f"/tmp/run-ord-{i}"),
        )
    pending = await store.pending_runs()
    ord_rows = [r for r in pending if r.run_id.startswith("run-ord-")]
    assert [r.run_id for r in ord_rows] == ["run-ord-0", "run-ord-1", "run-ord-2"]


# ---------------------------------------------------------------------------
# RunStore claim / record_pending / reset_stale_claims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_record_pending_appears_in_pending_runs(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="pending-parent")
    await store.record_pending(
        run_id="run-pending-1",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-pending-1"),
    )
    pending = await store.pending_runs()
    row = next(r for r in pending if r.run_id == "run-pending-1")
    assert row.status == "pending"


@pytest.mark.asyncio
async def test_run_claim_transitions_pending_to_running(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="claim-parent")
    await store.record_pending(
        run_id="run-claim-1",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-claim-1"),
    )
    assert await store.claim("run-claim-1", "worker-a") is True
    row = await store.get_run("run-claim-1")
    assert row is not None
    assert row.status == "running"
    assert row.worker_id == "worker-a"
    assert row.claimed_at is not None


@pytest.mark.asyncio
async def test_run_claim_returns_false_for_second_caller(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="claim-parent-2")
    await store.record_pending(
        run_id="run-claim-2",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-claim-2"),
    )
    assert await store.claim("run-claim-2", "worker-a") is True
    assert await store.claim("run-claim-2", "worker-b") is False


@pytest.mark.asyncio
async def test_run_claim_returns_false_for_non_pending(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="claim-parent-3")
    await store.record_started(
        run_id="run-claim-3",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-claim-3"),
    )
    assert await store.claim("run-claim-3", "worker-a") is False


@pytest.mark.asyncio
async def test_run_claim_concurrency_only_one_wins(sqlite_backend: SQLiteStorageBackend) -> None:
    import asyncio

    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="claim-race")
    await store.record_pending(
        run_id="run-claim-race",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-claim-race"),
    )

    results = await asyncio.gather(
        *[store.claim("run-claim-race", f"worker-{i}") for i in range(8)]
    )
    assert sum(1 for ok in results if ok) == 1


@pytest.mark.asyncio
async def test_run_reset_stale_claims(sqlite_backend: SQLiteStorageBackend) -> None:
    import asyncio

    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="stale-parent")
    await store.record_pending(
        run_id="run-stale-1",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-stale-1"),
    )
    assert await store.claim("run-stale-1", "worker-stale") is True
    await asyncio.sleep(0.02)
    stale = await store.list_stale_claims(stale_after_ms=10)
    assert len(stale) == 1
    assert stale[0].run_id == "run-stale-1"
    reset = await store.reset_stale_claims(stale_after_ms=10)
    assert reset == 1
    row = await store.get_run("run-stale-1")
    assert row is not None
    assert row.status == "pending"
    assert row.worker_id is None


@pytest.mark.asyncio
async def test_run_reset_stale_claim_skips_renewed_lease(
    sqlite_backend: SQLiteStorageBackend,
) -> None:
    import asyncio

    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="stale-renew-parent")
    await store.record_pending(
        run_id="run-stale-renew",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-stale-renew"),
    )
    assert await store.claim("run-stale-renew", "worker-stale") is True
    await asyncio.sleep(0.02)
    stale = await store.list_stale_claims(stale_after_ms=10)
    assert len(stale) == 1
    assert await store.renew_claim("run-stale-renew", "worker-stale") is True
    assert (
        await store.reset_stale_claim(
            "run-stale-renew",
            10,
            worker_id="worker-stale",
        )
        is False
    )
    row = await store.get_run("run-stale-renew")
    assert row is not None
    assert row.status == "running"
    assert row.worker_id == "worker-stale"


@pytest.mark.asyncio
async def test_run_renew_claim_extends_lease(sqlite_backend: SQLiteStorageBackend) -> None:
    import asyncio

    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="renew-parent")
    await store.record_pending(
        run_id="run-renew-1",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-renew-1"),
    )
    assert await store.claim("run-renew-1", "worker-renew") is True
    before = await store.get_run("run-renew-1")
    assert before is not None and before.claimed_at is not None
    await asyncio.sleep(0.02)
    assert await store.renew_claim("run-renew-1", "worker-renew") is True
    after = await store.get_run("run-renew-1")
    assert after is not None and after.claimed_at is not None
    assert after.claimed_at >= before.claimed_at
    assert await store.list_stale_claims(stale_after_ms=10) == []


@pytest.mark.asyncio
async def test_run_record_completed_requires_owner_when_worker_id_set(
    sqlite_backend: SQLiteStorageBackend,
) -> None:
    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="owner-parent")
    await store.record_pending(
        run_id="run-owner-1",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-owner-1"),
    )
    assert await store.claim("run-owner-1", "worker-a") is True
    assert await store.record_completed(
        "run-owner-1", '{"from":"b"}', worker_id="worker-b"
    ) is False
    row = await store.get_run("run-owner-1")
    assert row is not None
    assert row.status == "running"
    assert await store.record_completed(
        "run-owner-1", '{"from":"a"}', worker_id="worker-a"
    ) is True
    row = await store.get_run("run-owner-1")
    assert row is not None
    assert row.status == "completed"
    assert row.result_json == '{"from":"a"}'


@pytest.mark.asyncio
async def test_run_record_failed_ignored_after_reclaim(
    sqlite_backend: SQLiteStorageBackend,
) -> None:
    import asyncio

    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="reclaim-parent")
    await store.record_pending(
        run_id="run-reclaim-1",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=env,
        scratch_dir=Path("/tmp/run-reclaim-1"),
    )
    assert await store.claim("run-reclaim-1", "worker-old") is True
    await asyncio.sleep(0.02)
    assert await store.reset_stale_claims(stale_after_ms=10) == 1
    assert await store.claim("run-reclaim-1", "worker-new") is True
    assert await store.record_failed(
        "run-reclaim-1", "stale worker finishing", worker_id="worker-old"
    ) is False
    row = await store.get_run("run-reclaim-1")
    assert row is not None
    assert row.status == "running"
    assert row.worker_id == "worker-new"


def test_factory_raises_runtime_error_for_firestore_without_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "monkeybot.core.persistence.firestore", None)

    with pytest.raises(RuntimeError, match="google-cloud-firestore is not installed"):
        create_storage_backend("firestore://my-project/(default)")


def test_parse_firestore_config_from_url() -> None:
    from monkeybot.core.persistence.backends import _parse_firestore_config

    cfg = _parse_firestore_config("firestore://aurigaos/production?prefix=mb")
    assert cfg is not None
    assert cfg.project == "aurigaos"
    assert cfg.database == "production"
    assert cfg.prefix == "mb"
