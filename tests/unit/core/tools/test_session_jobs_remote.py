"""Remote background jobs on the session table: wake, kill, loss, timeout."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from monkeybot.core.tools.background_jobs import JobStatus
from monkeybot.core.tools.session_jobs import SessionJobs


class _Driver:
    def __init__(self) -> None:
        self.status = AsyncMock(return_value=MagicMock(running=True, exit_code=None, error=None))
        self.interrupt = AsyncMock()
        self.pull_logs = AsyncMock()


def _table(on_wake: object | None = None) -> SessionJobs:
    return SessionJobs(
        max_jobs=2,
        on_wake=on_wake,  # type: ignore[arg-type]
        wake_debounce_s=0,
    )


async def _job(table: SessionJobs, tmp_path: Path, *, timeout: int = 30, ceiling: bool = False):
    job = table.allocate(
        "python3",
        ["-c", "print(1)"],
        effective=timeout,
        hit_ceiling=ceiling,
        log_dir=tmp_path,
    )
    job.remote_id = "remote-1"
    return job


@pytest.mark.case("T1-SBX-09")
@pytest.mark.asyncio
async def test_remote_exit_wakes_the_session(tmp_path: Path) -> None:
    woken: list[list[tuple[str, str]]] = []

    async def on_wake(batch: list[tuple[str, str]]) -> bool:
        woken.append(batch)
        return True

    table = _table(on_wake)
    driver = _Driver()
    job = await _job(table, tmp_path)
    await table.adopt_remote(job, driver, poll_interval=0.01)
    driver.status.return_value = MagicMock(running=False, exit_code=0, error=None)
    await asyncio.wait_for(table._remotes[job.job_id].done.wait(), timeout=1)
    for _ in range(5):
        await asyncio.sleep(0)
    assert woken
    assert woken[0][0][0] == job.job_id
    assert job.status is JobStatus.EXITED
    assert job.exit_code == 0


@pytest.mark.case("T1-SBX-10")
@pytest.mark.asyncio
async def test_kill_interrupts_remote_job(tmp_path: Path) -> None:
    table = _table()
    driver = _Driver()
    job = await _job(table, tmp_path)
    await table.adopt_remote(job, driver, poll_interval=30)
    killed = await table.kill(job.job_id, reason="killed")
    driver.interrupt.assert_awaited_with("remote-1")
    assert killed.status is JobStatus.KILLED
    await table.aclose("session closed")


@pytest.mark.case("T1-SBX-11")
@pytest.mark.asyncio
async def test_status_failure_interrupts_and_lost_when_interrupt_fails(tmp_path: Path) -> None:
    table = _table()
    driver = _Driver()
    driver.status.side_effect = RuntimeError("status endpoint missing")
    driver.interrupt.side_effect = RuntimeError("interrupt failed")
    job = await _job(table, tmp_path)
    await table.adopt_remote(job, driver, poll_interval=30)
    lost = await table.get_status(job.job_id)
    driver.interrupt.assert_awaited()
    assert lost.status is JobStatus.LOST
    assert "status endpoint missing" in (lost.error or "")
    await table.aclose("session closed")


@pytest.mark.case("T1-SBX-11")
@pytest.mark.asyncio
async def test_exit_without_code_is_lost(tmp_path: Path) -> None:
    table = _table()
    driver = _Driver()
    driver.status.return_value = MagicMock(running=False, exit_code=None, error=None)
    job = await _job(table, tmp_path)
    await table.adopt_remote(job, driver, poll_interval=30)
    lost = await table.get_status(job.job_id)
    assert lost.status is JobStatus.LOST
    assert "without an exit code" in (lost.error or "")
    await table.aclose("session closed")


@pytest.mark.case("T1-SBX-14")
@pytest.mark.asyncio
async def test_remote_job_past_deadline_is_killed(tmp_path: Path) -> None:
    table = _table()
    driver = _Driver()
    job = await _job(table, tmp_path, timeout=0, ceiling=False)
    await table.adopt_remote(job, driver, poll_interval=30)
    expired = await table.get_status(job.job_id)
    driver.interrupt.assert_awaited_with("remote-1")
    assert expired.status is JobStatus.KILLED
    assert expired.error == "Command exceeded 0s timeout"
    await table.aclose("session closed")


@pytest.mark.case("T1-SBX-15")
@pytest.mark.asyncio
async def test_session_close_interrupts_remote_jobs(tmp_path: Path) -> None:
    table = _table()
    driver = _Driver()
    job = await _job(table, tmp_path)
    await table.adopt_remote(job, driver, poll_interval=30)
    killed = await table.aclose("session closed")
    driver.interrupt.assert_awaited_with("remote-1")
    assert killed
    assert killed[0].status is JobStatus.KILLED
    assert await table.aclose("session closed") == []
