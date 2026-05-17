"""Tests for StorageBackend protocol implementations and the create_storage_backend factory."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio

from monkeybot.core.llm.usage import Usage, UsageSummary
from monkeybot.core.llm.provider import Message
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.persistence.backends import create_storage_backend
from monkeybot.core.persistence.durable_runs import SubagentEnvelope, SubagentRunRow


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
        memory_path="/mem",
        parent_run_id=parent_run_id,
    )


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


def test_factory_returns_sqlite_backend_for_sqlite_url() -> None:
    backend = create_storage_backend("sqlite:///:memory:")
    assert isinstance(backend, SQLiteStorageBackend)


def test_factory_raises_value_error_for_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="Unsupported DB URL scheme"):
        create_storage_backend("mysql://localhost/db")


def test_factory_raises_runtime_error_for_postgres_without_asyncpg() -> None:
    try:
        import asyncpg  # noqa: F401
        pytest.skip("asyncpg is installed; cannot test missing-dep error path")
    except ImportError:
        pass

    with pytest.raises(RuntimeError, match="asyncpg is not installed"):
        create_storage_backend("postgresql://localhost/db")


def test_factory_postgres_scheme_alias_without_asyncpg() -> None:
    """``postgres://`` must hit the Postgres branch (not ValueError), same as postgresql://."""
    try:
        import asyncpg  # noqa: F401
        pytest.skip("asyncpg is installed; cannot test missing-dep error path")
    except ImportError:
        pass

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
async def test_run_record_started_appears_in_pending_runs(sqlite_backend: SQLiteStorageBackend) -> None:
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
    assert "run-002" in run_ids


@pytest.mark.asyncio
async def test_run_record_completed_removes_from_pending(sqlite_backend: SQLiteStorageBackend) -> None:
    store = sqlite_backend.runs()
    env = _make_envelope(parent_run_id="p3")
    await store.record_started(
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
        await store.record_started(
            run_id=f"run-ord-{i}",
            parent_run_id=None,
            script="subagents/x.py",
            envelope=env,
            scratch_dir=Path(f"/tmp/run-ord-{i}"),
        )
    pending = await store.pending_runs()
    ord_rows = [r for r in pending if r.run_id.startswith("run-ord-")]
    assert [r.run_id for r in ord_rows] == ["run-ord-0", "run-ord-1", "run-ord-2"]
