"""Tests for the poll-and-claim subagent worker pool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from monkeybot.core.persistence.durable_runs import SubagentEnvelope
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.runtime.events import TurnComplete, UsageTotals
from monkeybot.core.subagents import worker_pool


@pytest_asyncio.fixture
async def sqlite_backend():
    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    yield backend
    await backend.close()


def _make_envelope(parent_run_id: str = "parent-1") -> SubagentEnvelope:
    return SubagentEnvelope(
        task="do work",
        context="ctx",
        memory_storage_uri="local:///mem",
        parent_run_id=parent_run_id,
    )


@pytest.mark.asyncio
async def test_worker_pool_executes_claimed_run_once(
    sqlite_backend: SQLiteStorageBackend,
    tmp_path: Path,
) -> None:
    store = sqlite_backend.runs()
    scratch = tmp_path / "scratch"
    env = _make_envelope()
    await store.record_pending(
        run_id="worker-run-1",
        parent_run_id="parent",
        script="subagents/x.py",
        envelope=env,
        scratch_dir=scratch,
    )
    assert await store.claim("worker-run-1", "worker-a") is True

    executed: list[str] = []

    async def _fake_spawn(script: str, envelope, *, scratch_dir: Path, subprocess_exec=None):
        executed.append(envelope.task)
        yield TurnComplete(request_id="req-1", usage=UsageTotals())

    with patch.object(worker_pool, "spawn_subagent", side_effect=_fake_spawn):
        row = await store.get_run("worker-run-1")
        assert row is not None
        await worker_pool.execute_claimed_run(
            store,
            row,
            script=tmp_path / "worker.py",
        )

    row = await store.get_run("worker-run-1")
    assert row is not None
    assert row.status == "completed"
    assert executed == ["do work"]


@pytest.mark.asyncio
async def test_worker_loop_claims_and_executes_pending_run(
    sqlite_backend: SQLiteStorageBackend,
    tmp_path: Path,
) -> None:
    store = sqlite_backend.runs()
    scratch = tmp_path / "scratch"
    env = _make_envelope()
    await store.record_pending(
        run_id="worker-run-2",
        parent_run_id="parent",
        script="subagents/x.py",
        envelope=env,
        scratch_dir=scratch,
    )

    execution_count = 0

    async def _fake_spawn(script: str, envelope, *, scratch_dir: Path, subprocess_exec=None):
        nonlocal execution_count
        execution_count += 1
        yield TurnComplete(request_id="req-2", usage=UsageTotals())

    with (
        patch.object(worker_pool, "spawn_subagent", side_effect=_fake_spawn),
        patch.object(worker_pool.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await worker_pool.run_worker_loop(
            sqlite_backend,
            worker_id="worker-a",
            script=tmp_path / "worker.py",
            poll_interval_s=0.01,
            concurrency=1,
            stale_claim_ms=600_000,
        )

    row = await store.get_run("worker-run-2")
    assert row is not None
    assert row.status == "completed"
    assert execution_count == 1
