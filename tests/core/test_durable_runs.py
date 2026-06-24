"""Tests for durable run persistence and schema CLI."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from monkeybot.core.persistence.durable_runs import SQLiteRunStore, SubagentEnvelope, SubagentRunRow
from monkeybot.core.persistence.sqlite import apply_schema, open_connection


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


async def _apply_schema(conn) -> None:
    await apply_schema(conn)


@pytest_asyncio.fixture
async def durable_conn():
    conn = await open_connection("sqlite:///:memory:")
    await _apply_schema(conn)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_pending_runs_excludes_running(durable_conn) -> None:
    store = SQLiteRunStore(durable_conn)
    envelope = SubagentEnvelope(
        task="t",
        context="c",
        memory_storage_uri="local:///m",
        parent_run_id="p1",
    )
    await store.record_started(
        run_id="run-1",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=envelope,
        scratch_dir=Path("/tmp/run-1"),
    )
    assert await store.pending_runs() == []


@pytest.mark.asyncio
async def test_pending_runs_excludes_completed(durable_conn) -> None:
    store = SQLiteRunStore(durable_conn)
    envelope = SubagentEnvelope(
        task="t",
        context="c",
        memory_storage_uri="local:///m",
        parent_run_id="p1",
    )
    await store.record_started(
        run_id="run-2",
        parent_run_id=None,
        script="subagents/x.py",
        envelope=envelope,
        scratch_dir=Path("/tmp/run-2"),
    )
    await store.record_completed("run-2", '{"ok":true}')
    assert await store.pending_runs() == []


@pytest.mark.asyncio
async def test_get_run_round_trip(durable_conn) -> None:
    store = SQLiteRunStore(durable_conn)
    envelope = SubagentEnvelope(
        task="task-a",
        context="ctx",
        memory_storage_uri="local:///mem",
        parent_run_id="parent",
        model="gemini-2.5-flash",
    )
    scratch = Path("/data/runs/run-3")
    await store.record_started(
        run_id="run-3",
        parent_run_id="parent-top",
        script="subagents/y.py",
        envelope=envelope,
        scratch_dir=scratch,
    )
    row = await store.get_run("run-3")
    assert row is not None
    assert isinstance(row, SubagentRunRow)
    assert row.envelope_json == envelope.to_json()
    assert row.scratch_dir == str(scratch)


@pytest.mark.asyncio
async def test_record_failed_sets_status(durable_conn) -> None:
    store = SQLiteRunStore(durable_conn)
    envelope = SubagentEnvelope(
        task="t",
        context="c",
        memory_storage_uri="local:///m",
        parent_run_id="p1",
    )
    await store.record_started(
        run_id="run-4",
        parent_run_id=None,
        script="subagents/z.py",
        envelope=envelope,
        scratch_dir=Path("/tmp/run-4"),
    )
    await store.record_failed("run-4", "boom")
    row = await store.get_run("run-4")
    assert row is not None
    assert isinstance(row, SubagentRunRow)
    assert row.status == "failed"
    assert row.error_json
    assert "boom" in str(row.error_json)


@pytest.mark.integration
def test_create_schema_idempotent_twice(tmp_path: Path) -> None:
    repo = _repo_root()
    script = repo / "scripts" / "create_schema.py"
    db_file = tmp_path / "schema.db"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo / "src")
    cmd = [sys.executable, str(script), "--db", str(db_file)]
    r1 = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    assert r1.returncode == 0, r1.stderr
    r2 = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    assert r2.returncode == 0, r2.stderr

    con = sqlite3.connect(db_file)
    try:
        cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        names = {row[0] for row in cur.fetchall()}
        assert {"conversation_history", "subagent_runs", "turn_usage"}.issubset(names)
    finally:
        con.close()


@pytest.mark.integration
def test_create_schema_applies_wal(tmp_path: Path) -> None:
    repo = _repo_root()
    script = repo / "scripts" / "create_schema.py"
    db_file = tmp_path / "wal.db"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo / "src")
    subprocess.run(
        [sys.executable, str(script), "--db", str(db_file)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    con = sqlite3.connect(db_file)
    try:
        row = con.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        assert str(row[0]).lower() == "wal"
    finally:
        con.close()
