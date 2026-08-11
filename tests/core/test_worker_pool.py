"""Tests for the poll-and-claim subagent worker pool."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.persistence.durable_runs import SubagentEnvelope
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.runtime.events import TurnComplete, UsageTotals
from monkeybot.core.subagents import worker_pool
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.types import unwrap_tool_execution_result
from tests.core.test_core_tool_executor import _NoMCP, _ctx, _mem_sub, _stub_agent_md_for_tasks


@pytest_asyncio.fixture
async def sqlite_backend():
    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    yield backend
    await backend.close()


def _make_envelope(parent_run_id: str = "parent-1", *, task: str = "do work") -> SubagentEnvelope:
    return SubagentEnvelope(
        task=task,
        context="ctx",
        memory_storage_uri="local:///mem",
        parent_run_id=parent_run_id,
    )


@pytest.mark.asyncio
async def test_kill_scratch_subagent_reads_pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "subagent.pid").write_text("424242\n", encoding="utf-8")
    kills: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        kills.append((pid, sig))

    monkeypatch.setattr(worker_pool.os, "kill", _fake_kill)
    worker_pool._kill_scratch_subagent(scratch)
    assert kills == [(424242, 9)]


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

    async def _fake_spawn(
        script: str,
        envelope,
        *,
        scratch_dir: Path,
        subprocess_exec=None,
        on_event=None,
        extra_env=None,
    ):
        del script, scratch_dir, subprocess_exec, on_event, extra_env
        executed.append(envelope.task)
        yield TurnComplete(request_id="req-1", usage=UsageTotals())

    with patch.object(worker_pool, "spawn_subagent", side_effect=_fake_spawn):
        row = await store.get_run("worker-run-1")
        assert row is not None
        await worker_pool.execute_claimed_run(
            store,
            row,
            script=tmp_path / "worker.py",
            worker_id="worker-a",
        )

    row = await store.get_run("worker-run-1")
    assert row is not None
    assert row.status == "completed"
    assert executed == ["do work"]


@pytest.mark.asyncio
async def test_execute_claimed_run_times_out_and_records_failure(
    sqlite_backend: SQLiteStorageBackend,
    tmp_path: Path,
) -> None:
    store = sqlite_backend.runs()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = _make_envelope()
    await store.record_pending(
        run_id="worker-run-timeout",
        parent_run_id="parent",
        script="subagents/x.py",
        envelope=env,
        scratch_dir=scratch,
    )
    assert await store.claim("worker-run-timeout", "worker-a") is True

    async def _hanging_spawn(
        script: str,
        envelope,
        *,
        scratch_dir: Path,
        subprocess_exec=None,
        on_event=None,
        extra_env=None,
    ):
        del script, envelope, scratch_dir, subprocess_exec, on_event, extra_env
        await asyncio.sleep(3600)
        yield TurnComplete(request_id="req-never", usage=UsageTotals())

    with patch.object(worker_pool, "spawn_subagent", side_effect=_hanging_spawn):
        row = await store.get_run("worker-run-timeout")
        assert row is not None
        await worker_pool.execute_claimed_run(
            store,
            row,
            script=tmp_path / "worker.py",
            worker_id="worker-a",
            timeout_sec=0.05,
        )

    row = await store.get_run("worker-run-timeout")
    assert row is not None
    assert row.status == "failed"
    assert row.error_json is not None
    assert "exit_reason=timeout" in row.error_json
    assert "progress.jsonl" in row.error_json


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

    async def _fake_spawn(
        script: str,
        envelope,
        *,
        scratch_dir: Path,
        subprocess_exec=None,
        on_event=None,
        extra_env=None,
    ):
        del script, scratch_dir, subprocess_exec, on_event, extra_env
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


@pytest.mark.asyncio
async def test_worker_pool_does_not_claim_beyond_concurrency(
    sqlite_backend: SQLiteStorageBackend,
    tmp_path: Path,
) -> None:
    store = sqlite_backend.runs()
    scratch = tmp_path / "scratch"

    for run_id, task in (("worker-run-a", "first"), ("worker-run-b", "second")):
        await store.record_pending(
            run_id=run_id,
            parent_run_id="parent",
            script="subagents/x.py",
            envelope=_make_envelope(task=task),
            scratch_dir=scratch / run_id,
        )

    first_started = asyncio.Event()
    unblock_first = asyncio.Event()

    async def _fake_spawn(
        script: str,
        envelope,
        *,
        scratch_dir: Path,
        subprocess_exec=None,
        on_event=None,
        extra_env=None,
    ):
        del script, scratch_dir, subprocess_exec, on_event, extra_env
        if envelope.task == "first":
            first_started.set()
            await unblock_first.wait()
        yield TurnComplete(request_id="req-block", usage=UsageTotals())

    loop_task: asyncio.Task[None] | None = None
    with patch.object(worker_pool, "spawn_subagent", side_effect=_fake_spawn):
        loop_task = asyncio.create_task(
            worker_pool.run_worker_loop(
                sqlite_backend,
                worker_id="worker-a",
                script=tmp_path / "worker.py",
                poll_interval_s=0.01,
                concurrency=1,
                stale_claim_ms=600_000,
            )
        )

        try:
            await asyncio.wait_for(first_started.wait(), timeout=2.0)
            row_b = await store.get_run("worker-run-b")
            assert row_b is not None
            assert row_b.status == "pending"

            unblock_first.set()
            for _ in range(200):
                row_a = await store.get_run("worker-run-a")
                row_b = await store.get_run("worker-run-b")
                assert row_a is not None and row_b is not None
                if row_a.status == "completed" and row_b.status == "completed":
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("expected both runs to complete")
        finally:
            assert loop_task is not None
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task


@pytest.mark.asyncio
async def test_shutdown_worker_pool_fails_in_flight_run(
    sqlite_backend: SQLiteStorageBackend,
    tmp_path: Path,
) -> None:
    store = sqlite_backend.runs()
    scratch = tmp_path / "scratch"
    await store.record_pending(
        run_id="worker-run-shutdown",
        parent_run_id="parent",
        script="subagents/x.py",
        envelope=_make_envelope(task="blocked"),
        scratch_dir=scratch,
    )

    run_started = asyncio.Event()
    hang_forever = asyncio.Event()

    async def _fake_spawn(
        script: str,
        envelope,
        *,
        scratch_dir: Path,
        subprocess_exec=None,
        on_event=None,
        extra_env=None,
    ):
        del script, scratch_dir, subprocess_exec, on_event, extra_env
        run_started.set()
        await hang_forever.wait()
        yield TurnComplete(request_id="req-block", usage=UsageTotals())

    active_runs: set[str] = set()
    loop_task = asyncio.create_task(
        worker_pool.run_worker_loop(
            sqlite_backend,
            worker_id="worker-a",
            script=tmp_path / "worker.py",
            poll_interval_s=0.01,
            concurrency=1,
            stale_claim_ms=600_000,
            active_runs=active_runs,
        )
    )
    handle = worker_pool.WorkerPoolHandle(
        task=loop_task,
        backend=sqlite_backend,
        active_runs=active_runs,
        worker_id="worker-a",
    )

    with patch.object(worker_pool, "spawn_subagent", side_effect=_fake_spawn):
        await asyncio.wait_for(run_started.wait(), timeout=2.0)
        await worker_pool.shutdown_worker_pool(handle)

    row = await store.get_run("worker-run-shutdown")
    assert row is not None
    assert row.status == "failed"
    assert row.error_json is not None
    assert "cancelled" in row.error_json.lower()


@pytest.mark.asyncio
async def test_queue_mode_enqueue_then_worker_executes_e2e(
    sqlite_backend: SQLiteStorageBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MONKEYBOT_TASK_QUEUE=1: CoreToolExecutor enqueues, worker claims and executes."""
    monkeypatch.setenv("MONKEYBOT_TASK_QUEUE", "1")
    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker_script = root / "subagent_worker.py"
    worker_script.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker_script))

    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
        run_store=sqlite_backend.runs(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="c-e2e",
                name="task",
                args={"task": "e2e queued", "context": "ctx"},
            ),
            ctx=_ctx(),
        )
    )
    assert err is not None and out is None
    run_id = json.loads(err)["details"]["run_id"]

    execution_count = 0

    async def _fake_spawn(
        script: str,
        envelope,
        *,
        scratch_dir: Path,
        subprocess_exec=None,
        on_event=None,
        extra_env=None,
    ):
        del script, scratch_dir, subprocess_exec, on_event, extra_env
        nonlocal execution_count
        execution_count += 1
        assert envelope.task == "e2e queued"
        yield TurnComplete(request_id="req-e2e", usage=UsageTotals())

    with (
        patch.object(worker_pool, "spawn_subagent", side_effect=_fake_spawn),
        patch.object(worker_pool.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await worker_pool.run_worker_loop(
            sqlite_backend,
            worker_id="worker-e2e",
            script=worker_script,
            poll_interval_s=0.01,
            concurrency=1,
            stale_claim_ms=600_000,
        )

    row = await sqlite_backend.runs().get_run(run_id)
    assert row is not None
    assert row.status == "completed"
    assert execution_count == 1


@pytest.mark.asyncio
async def test_stale_worker_does_not_overwrite_reclaimed_run(
    sqlite_backend: SQLiteStorageBackend,
    tmp_path: Path,
) -> None:
    store = sqlite_backend.runs()
    scratch = tmp_path / "scratch"
    env = _make_envelope(task="long task")
    await store.record_pending(
        run_id="worker-run-stale",
        parent_run_id="parent",
        script="subagents/x.py",
        envelope=env,
        scratch_dir=scratch,
    )
    assert await store.claim("worker-run-stale", "worker-a") is True
    stale_row = await store.get_run("worker-run-stale")
    assert stale_row is not None

    await asyncio.sleep(0.02)
    await store.reset_stale_claims(stale_after_ms=1)
    assert await store.claim("worker-run-stale", "worker-b") is True

    async def _fake_spawn(
        script: str,
        envelope,
        *,
        scratch_dir: Path,
        subprocess_exec=None,
        on_event=None,
        extra_env=None,
    ):
        del script, scratch_dir, subprocess_exec, on_event, extra_env
        yield TurnComplete(request_id="req-stale", usage=UsageTotals())

    with patch.object(worker_pool, "spawn_subagent", side_effect=_fake_spawn):
        await worker_pool.execute_claimed_run(
            store,
            stale_row,
            script=tmp_path / "worker.py",
            worker_id="worker-a",
        )

    row = await store.get_run("worker-run-stale")
    assert row is not None
    assert row.status == "running"
    assert row.worker_id == "worker-b"
    assert row.result_json is None
