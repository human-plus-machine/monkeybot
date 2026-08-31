"""Tests for the poll-and-claim subagent worker pool."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.persistence.durable_runs import SubagentEnvelope
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.runtime.events import TurnComplete, UsageTotals
from monkeybot.core.subagents import worker_pool
from monkeybot.core.subprocess_groups import stop_subagent_process
from monkeybot.core import subprocess_groups
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
    (scratch / "subagent.pid").write_text("424242\nps:Wed Aug 11 12:00:00 2026\n", encoding="utf-8")
    kills: list[tuple[int, int]] = []
    killpgs: list[tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        kills.append((pid, sig))

    def _fake_killpg(pgid: int, sig: int) -> None:
        killpgs.append((pgid, sig))

    monkeypatch.setattr(subprocess_groups, "SUPPORTS_PROCESS_GROUPS", True)
    monkeypatch.setattr(
        worker_pool,
        "_process_identity",
        lambda pid: "ps:Wed Aug 11 12:00:00 2026" if pid == 424242 else None,
    )
    monkeypatch.setattr(worker_pool.os, "kill", _fake_kill)
    monkeypatch.setattr(worker_pool.os, "killpg", _fake_killpg)
    worker_pool._kill_scratch_subagent(scratch)
    assert killpgs == [(424242, 9)]
    assert kills == []


@pytest.mark.asyncio
async def test_kill_scratch_subagent_skips_pid_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "subagent.pid").write_text(
        "424242\nps:original-start\n", encoding="utf-8"
    )
    killpgs: list[tuple[int, int]] = []

    monkeypatch.setattr(subprocess_groups, "SUPPORTS_PROCESS_GROUPS", True)
    monkeypatch.setattr(
        worker_pool,
        "_process_identity",
        lambda pid: "ps:reused-start" if pid == 424242 else None,
    )
    monkeypatch.setattr(
        worker_pool.os,
        "killpg",
        lambda pgid, sig: killpgs.append((pgid, sig)),
    )
    worker_pool._kill_scratch_subagent(scratch)
    assert killpgs == []


@pytest.mark.asyncio
async def test_kill_scratch_subagent_skips_legacy_pid_only_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "subagent.pid").write_text("424242\n", encoding="utf-8")
    killpgs: list[tuple[int, int]] = []

    monkeypatch.setattr(subprocess_groups, "SUPPORTS_PROCESS_GROUPS", True)
    monkeypatch.setattr(
        worker_pool,
        "_process_identity",
        lambda pid: "ps:anything",
    )
    monkeypatch.setattr(
        worker_pool.os,
        "killpg",
        lambda pgid, sig: killpgs.append((pgid, sig)),
    )
    worker_pool._kill_scratch_subagent(scratch)
    assert killpgs == []


@pytest.mark.asyncio
async def test_write_subagent_pid_records_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(
        worker_pool,
        "_process_identity",
        lambda pid: f"ps:token-{pid}",
    )
    worker_pool._write_subagent_pid(scratch, 99)
    assert (scratch / "subagent.pid").read_text(encoding="utf-8") == "99\nps:token-99\n"


@pytest.mark.asyncio
async def test_kill_scratch_subagent_kills_when_leader_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "subagent.pid").write_text(
        "424242\nps:original-start\n", encoding="utf-8"
    )
    killpgs: list[tuple[int, int]] = []

    monkeypatch.setattr(subprocess_groups, "SUPPORTS_PROCESS_GROUPS", True)
    monkeypatch.setattr(worker_pool, "_process_identity", lambda pid: None)
    monkeypatch.setattr(
        worker_pool.os,
        "killpg",
        lambda pgid, sig: killpgs.append((pgid, sig)),
    )
    worker_pool._kill_scratch_subagent(scratch)
    assert killpgs == [(424242, 9)]


@pytest.mark.asyncio
async def test_stop_subagent_process_kills_group_after_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pgid known at spawn time must still be killpg'd even once the leader
    is already reaped. The tree walk must not run against the freed PID — that
    can ``killpg`` an unrelated recycled process — so the captured ``pgid`` is
    the only signal used.
    """
    signals: list[tuple[str, int, int]] = []

    class _FakeProc:
        pid = 111
        returncode = 0  # leader already exited/reaped

        def terminate(self) -> None:
            raise AssertionError("should use killpg")

        def kill(self) -> None:
            raise AssertionError("should use killpg")

        async def wait(self) -> int:
            return 0

    def _iter_must_not_run(root: int) -> list[int]:
        raise AssertionError(f"must not walk reaped leader pid {root}")

    def _fake_killpg(pgid: int, sig: int) -> None:
        signals.append(("killpg", pgid, int(sig)))

    monkeypatch.setattr(subprocess_groups, "SUPPORTS_PROCESS_GROUPS", True)
    monkeypatch.setattr(subprocess_groups, "iter_process_tree", _iter_must_not_run)
    monkeypatch.setattr(subprocess_groups.os, "killpg", _fake_killpg)
    monkeypatch.setattr(subprocess_groups.asyncio, "sleep", AsyncMock())

    proc = _FakeProc()
    await stop_subagent_process(proc, pgid=111)  # type: ignore[arg-type]
    assert ("killpg", 111, int(signal.SIGTERM)) in signals
    assert ("killpg", 111, int(signal.SIGKILL)) in signals


@pytest.mark.asyncio
async def test_stop_subagent_process_kills_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[str, int, int]] = []

    class _FakeProc:
        pid = 111
        returncode = None

        def terminate(self) -> None:
            raise AssertionError("should use killpg for process groups")

        def kill(self) -> None:
            signals.append(("kill", self.pid, 9))

        async def wait(self) -> int:
            self.returncode = -9
            return -9

    def _fake_killpg(pgid: int, sig: int) -> None:
        signals.append(("killpg", pgid, int(sig)))

    async def _fake_wait_for(awaitable, timeout=None):  # noqa: ANN001, ARG001
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError()

    monkeypatch.setattr(subprocess_groups, "SUPPORTS_PROCESS_GROUPS", True)
    monkeypatch.setattr(subprocess_groups, "iter_process_tree", lambda root: [root])
    monkeypatch.setattr(subprocess_groups.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(subprocess_groups.os, "killpg", _fake_killpg)
    monkeypatch.setattr(subprocess_groups.asyncio, "wait_for", _fake_wait_for)

    proc = _FakeProc()
    await stop_subagent_process(proc, pgid=111)  # type: ignore[arg-type]
    assert ("killpg", 111, int(signal.SIGTERM)) in signals
    assert ("killpg", 111, int(signal.SIGKILL)) in signals


@pytest.mark.skipif(sys.platform == "win32", reason="posix-only process tree walk")
def test_direct_child_pids_finds_real_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the non-``/proc`` fallback against a real child process.

    Forces the ``/proc`` branch off so this also covers platforms without it
    (e.g. macOS, where the old ``ps -o pid= -P`` invocation was invalid and
    silently returned no children).
    """
    monkeypatch.setattr(subprocess_groups.Path, "exists", lambda self: False)
    child = subprocess.Popen(["sleep", "5"])
    try:
        children = subprocess_groups._direct_child_pids(os.getpid())
        assert child.pid in children
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_process_group_id_returns_none_when_pid_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_pid: int) -> int:
        raise ProcessLookupError()

    monkeypatch.setattr(subprocess_groups, "SUPPORTS_PROCESS_GROUPS", True)
    monkeypatch.setattr(subprocess_groups.os, "getpgid", _boom)
    assert subprocess_groups.process_group_id(4242) is None


@pytest.mark.asyncio
async def test_stop_subagent_process_kills_nested_terminal_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested run_command sessions (own pg) must die when the subagent is stopped."""
    signals: list[tuple[int, int]] = []

    class _FakeProc:
        pid = 100
        returncode = None

        def terminate(self) -> None:
            raise AssertionError("should signal the process tree")

        def kill(self) -> None:
            raise AssertionError("should signal the process tree")

        async def wait(self) -> int:
            self.returncode = -9
            return -9

    def _fake_killpg(pgid: int, sig: int) -> None:
        signals.append((pgid, int(sig)))

    def _fake_getpgid(pid: int) -> int:
        return {100: 100, 200: 200, 300: 300}[pid]

    async def _fake_wait_for(awaitable, timeout=None):  # noqa: ANN001, ARG001
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise TimeoutError()

    monkeypatch.setattr(subprocess_groups, "SUPPORTS_PROCESS_GROUPS", True)
    monkeypatch.setattr(subprocess_groups, "iter_process_tree", lambda root: [100, 200, 300])
    monkeypatch.setattr(subprocess_groups.os, "killpg", _fake_killpg)
    monkeypatch.setattr(subprocess_groups.os, "getpgid", _fake_getpgid)
    monkeypatch.setattr(subprocess_groups.asyncio, "wait_for", _fake_wait_for)

    proc = _FakeProc()
    await stop_subagent_process(proc, pgid=100)  # type: ignore[arg-type]
    # Nested descendants (200, 300) must be signaled exactly once via the tree
    # walk. The root's own pgid (100) is signaled by both the tree walk and
    # the direct pgid kill (belt-and-suspenders for an already-dead leader),
    # so it may appear more than once.
    assert signals.count((300, int(signal.SIGTERM))) == 1
    assert signals.count((200, int(signal.SIGTERM))) == 1
    assert signals.count((100, int(signal.SIGTERM))) >= 1
    assert signals.count((300, int(signal.SIGKILL))) == 1
    assert signals.count((200, int(signal.SIGKILL))) == 1
    assert signals.count((100, int(signal.SIGKILL))) >= 1


@pytest.mark.asyncio
async def test_claim_heartbeat_swallows_renew_errors(
    sqlite_backend: SQLiteStorageBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = sqlite_backend.runs()

    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    store.renew_claim = boom  # type: ignore[method-assign]
    monkeypatch.setattr(worker_pool.asyncio, "sleep", AsyncMock())
    # Should return, not raise.
    await worker_pool._claim_heartbeat(store, "run-x", "worker-a", stale_claim_ms=40)


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


@pytest.mark.asyncio
async def test_reaper_only_kills_after_successful_reset(
    sqlite_backend: SQLiteStorageBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = sqlite_backend.runs()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = _make_envelope(task="renewed")
    await store.record_pending(
        run_id="worker-run-renew-reap",
        parent_run_id="parent",
        script="subagents/x.py",
        envelope=env,
        scratch_dir=scratch,
    )
    assert await store.claim("worker-run-renew-reap", "worker-live") is True
    await asyncio.sleep(0.02)

    kills: list[Path] = []
    monkeypatch.setattr(
        worker_pool,
        "_kill_scratch_subagent",
        lambda path: kills.append(path),
    )

    stale_rows = await store.list_stale_claims(stale_after_ms=10)
    assert len(stale_rows) == 1
    assert await store.renew_claim("worker-run-renew-reap", "worker-live") is True

    # Same gate as run_worker_loop: kill only if atomic reset wins.
    for stale in stale_rows:
        if not await store.reset_stale_claim(
            stale.run_id,
            10,
            worker_id=stale.worker_id,
        ):
            continue
        worker_pool._kill_scratch_subagent(Path(stale.scratch_dir))

    assert kills == []
    row = await store.get_run("worker-run-renew-reap")
    assert row is not None
    assert row.status == "running"
    assert row.worker_id == "worker-live"
