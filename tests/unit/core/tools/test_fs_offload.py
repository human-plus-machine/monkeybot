"""File tools must not stall the event loop, and a cancel must stop the scan."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.tools import fs_worker
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.fs_worker import (
    FsCancel,
    FsCancelledError,
    effective_cpus,
    pool_size,
)
from monkeybot.core.tools.read_ledger import FileReadLedger, stamp_of
from monkeybot.core.tools.types import unwrap_tool_execution_result
from monkeybot.core.tools.workspace_service import WorkspaceFileService
from testkit.context import tool_ctx
from testkit.mcp import NoMCP


def _executor(tmp_path: Path, *, read_ledger: FileReadLedger | None = None) -> CoreToolExecutor:
    skills = tmp_path / "skills"
    skills.mkdir()
    return CoreToolExecutor(
        workspace_root=tmp_path,
        memory=None,
        skills_path=skills,
        mcp=NoMCP(),
        read_ledger=read_ledger,
    )


def _patch_cgroup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    affinity: int,
    v2: str | None = None,
    v1_quota: str | None = None,
    v1_period: str | None = None,
) -> None:
    """Point cgroup reads at ``tmp_path`` and pin the affinity count."""
    monkeypatch.setattr(fs_worker, "_affinity_cpus", lambda: affinity)
    v2_path = tmp_path / "cpu.max"
    if v2 is None:
        v2_path = tmp_path / "missing-cpu.max"
    else:
        v2_path.write_text(v2, encoding="utf-8")
    quota_path = tmp_path / "cpu.cfs_quota_us"
    period_path = tmp_path / "cpu.cfs_period_us"
    if v1_quota is None:
        quota_path = tmp_path / "missing-quota"
    else:
        quota_path.write_text(v1_quota, encoding="utf-8")
    if v1_period is None:
        period_path = tmp_path / "missing-period"
    else:
        period_path.write_text(v1_period, encoding="utf-8")
    monkeypatch.setattr(fs_worker, "_CGROUP_V2_CPU_MAX", v2_path)
    monkeypatch.setattr(fs_worker, "_CGROUP_V1_QUOTA", quota_path)
    monkeypatch.setattr(fs_worker, "_CGROUP_V1_PERIOD", period_path)


async def test_grep_does_not_stall_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 0.5 s grep must leave the loop free to tick every 10 ms."""

    def slow_grep(self: WorkspaceFileService, pattern: str, **_kwargs: object) -> dict[str, object]:
        del self, pattern
        time.sleep(0.5)
        return {"ok": True, "matches": []}

    monkeypatch.setattr(WorkspaceFileService, "grep", slow_grep)
    executor = _executor(tmp_path)
    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    await executor.execute(
        call=ToolCall(call_id="g", name="grep", args={"pattern": "needle"}),
        ctx=tool_ctx(),
    )
    stop.set()
    await ticker
    assert gaps
    assert max(gaps) < 0.1


async def test_parallel_reads_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Four 0.3 s reads issued together finish well under their 1.2 s sum."""

    def slow_read(self: WorkspaceFileService, path: str, **_kwargs: object) -> dict[str, object]:
        del self
        time.sleep(0.3)
        return {
            "ok": True,
            "path": path,
            "content": "",
            "start_line": 1,
            "end_line": 0,
            "total_lines": 0,
            "truncated": False,
        }

    monkeypatch.setattr(WorkspaceFileService, "read_file", slow_read)
    executor = _executor(tmp_path)
    started = time.monotonic()
    await asyncio.gather(
        *(
            executor.execute(
                call=ToolCall(call_id=str(i), name="read_file", args={"path": f"f{i}.txt"}),
                ctx=tool_ctx(),
            )
            for i in range(4)
        )
    )
    elapsed = time.monotonic() - started
    assert elapsed < 0.9


async def test_cancel_kills_rg_and_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a grep kills the ripgrep child and does not start the Python walk."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("needle\n", encoding="utf-8")
    script = tmp_path / "rg"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, time\n"
        "with open(os.environ['RG_PID_FILE'], 'w', encoding='utf-8') as handle:\n"
        "    handle.write(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    pid_file = tmp_path / "rg.pid"
    monkeypatch.setenv("RG_PID_FILE", str(pid_file))
    monkeypatch.setattr(
        "monkeybot.core.tools.workspace_service.shutil.which",
        lambda _name: str(script),
    )
    python_calls = {"n": 0}
    real_python = WorkspaceFileService._grep_python

    def _track_python(self: WorkspaceFileService, *args: object, **kwargs: object) -> object:
        python_calls["n"] += 1
        return real_python(self, *args, **kwargs)

    monkeypatch.setattr(WorkspaceFileService, "_grep_python", _track_python)
    executor = _executor(workspace)
    task = asyncio.create_task(
        executor.execute(
            call=ToolCall(call_id="g", name="grep", args={"pattern": "needle"}),
            ctx=tool_ctx(),
        )
    )
    pid: int | None = None
    try:
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.02)
        assert pid_file.exists()
        pid = int(pid_file.read_text(encoding="utf-8"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _alive(pid):
            await asyncio.sleep(0.02)
        assert not _alive(pid)
        assert python_calls["n"] == 0
    finally:
        if pid is not None and _alive(pid):
            os.kill(pid, 9)
        if not task.done():
            task.cancel()


def test_cancelled_python_grep_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Python walker checks the cancel flag, so it does not keep scanning."""
    monkeypatch.setattr("monkeybot.core.tools.workspace_service.shutil.which", lambda _name: None)
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    cancel = FsCancel()
    cancel.cancel()
    token = fs_worker._current_cancel.set(cancel)
    try:
        with pytest.raises(FsCancelledError):
            WorkspaceFileService(tmp_path).grep("needle")
    finally:
        fs_worker._current_cancel.reset(token)


async def test_in_flight_grep_keeps_grant_after_revoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan keeps the roots it started with after a later call clears the shared service."""
    monkeypatch.setattr("monkeybot.core.tools.workspace_service.shutil.which", lambda _name: None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    granted = tmp_path / "granted"
    granted.mkdir()
    (granted / "notes.txt").write_text("TOKEN\n", encoding="utf-8")
    (granted / ".env").write_text("TOKEN\n", encoding="utf-8")
    executor = _executor(workspace)
    real = WorkspaceFileService._is_denied_extra_root_result

    def _clear_shared_then_filter(self: WorkspaceFileService, fp: Path) -> bool:
        executor._workspace.sync_extra_read_roots([])
        return real(self, fp)

    monkeypatch.setattr(
        WorkspaceFileService, "_is_denied_extra_root_result", _clear_shared_then_filter
    )
    ctx = tool_ctx()
    ctx.turn_path_grants.add(str(granted.resolve()))
    out, err = unwrap_tool_execution_result(
        await executor.execute(
            call=ToolCall(
                call_id="g",
                name="grep",
                args={
                    "pattern": "TOKEN",
                    "root": str(granted),
                    "respect_gitignore": False,
                },
            ),
            ctx=ctx,
        )
    )
    assert err is None and out is not None
    paths = [match["path"] for match in json.loads(out)["matches"]]
    assert any(path.endswith("notes.txt") for path in paths)
    assert not any(path.endswith(".env") for path in paths)


async def test_parallel_reads_are_all_recorded(tmp_path: Path) -> None:
    ledger = FileReadLedger()
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text(f"line {i}\n", encoding="utf-8")
    executor = _executor(tmp_path, read_ledger=ledger)
    await asyncio.gather(
        *(
            executor.execute(
                call=ToolCall(call_id=str(i), name="read_file", args={"path": f"f{i}.txt"}),
                ctx=tool_ctx(),
            )
            for i in range(10)
        )
    )
    for i in range(10):
        path = tmp_path / f"f{i}.txt"
        assert ledger.check_write(f"f{i}.txt", stamp_of(path)) is None


def test_pool_size_follows_stdlib_formula() -> None:
    assert pool_size(1) == 5
    assert pool_size(10) == 14
    assert pool_size(28) == 32
    assert pool_size(64) == 32


def test_effective_cpus_v2_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cgroup(monkeypatch, tmp_path, affinity=64, v2="150000 100000\n")
    assert effective_cpus() == 2
    assert pool_size(effective_cpus()) == 6


def test_effective_cpus_v2_max_is_no_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cgroup(
        monkeypatch,
        tmp_path,
        affinity=64,
        v2="max 100000\n",
        v1_quota="100000",
        v1_period="100000",
    )
    assert effective_cpus() == 64
    assert pool_size(effective_cpus()) == 32


def test_effective_cpus_v1_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cgroup(
        monkeypatch,
        tmp_path,
        affinity=64,
        v1_quota="200000",
        v1_period="100000",
    )
    assert effective_cpus() == 2
    assert pool_size(effective_cpus()) == 6


def test_effective_cpus_v1_unlimited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cgroup(
        monkeypatch,
        tmp_path,
        affinity=64,
        v1_quota="-1",
        v1_period="100000",
    )
    assert effective_cpus() == 64
    assert pool_size(effective_cpus()) == 32


def test_effective_cpus_missing_cgroup_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_cgroup(monkeypatch, tmp_path, affinity=64)
    assert effective_cpus() == 64
    assert pool_size(effective_cpus()) == 32


def test_effective_cpus_affinity_wins_over_a_wider_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_cgroup(monkeypatch, tmp_path, affinity=4, v2="800000 100000\n")
    assert effective_cpus() == 4
    assert pool_size(effective_cpus()) == 8


def test_one_cpu_container_pool_is_five(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cgroup(monkeypatch, tmp_path, affinity=64, v2="100000 100000\n")
    assert effective_cpus() == 1
    assert pool_size(effective_cpus()) == 5


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
