"""Session-owned background commands survive the turn that started them."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.runtime.wake_budget import WakeBudget
from monkeybot.core.subagents.subagent_proto import SubagentEnvelope
from monkeybot.core.subagents.supervisor import SubagentSpawn, SubagentSupervisor
from monkeybot.core.tools.background_jobs import JobStatus
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.session_jobs import SessionJobs
from monkeybot.core.tools.terminal import TerminalExecutor
from testkit.context import tool_ctx


class _NoMCP:
    def split_prefixed_tool(self, name: str) -> None:
        del name
        return None


def _ctx(tmp_path: Path) -> TurnContext:
    return TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
        workspace_root=tmp_path,
    )


def _executor(tmp_path: Path, jobs: SessionJobs) -> CoreToolExecutor:
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    return CoreToolExecutor(
        workspace_root=tmp_path,
        memory=None,
        skills_path=skills,
        mcp=_NoMCP(),  # type: ignore[arg-type]
        session_jobs=jobs,
    )


def _spec(run_id: str) -> SubagentSpawn:
    return SubagentSpawn(
        script=Path("worker.py"),
        envelope=SubagentEnvelope(
            task="t",
            context="",
            memory_storage_uri="",
            parent_run_id="p",
            parent_session_id="sess",
        ),
        scratch=Path("/tmp"),
        child_env={},
        timeout=5,
        ctx=tool_ctx(),
        call=ToolCall(call_id="c", name="task", args={}),
        run_id=run_id,
        child_thread_id=f"subagent:sess:{run_id}",
        subagent_type=None,
        task="t",
        session_id="sess",
        publisher=None,
    )


async def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for condition")


@pytest.mark.asyncio
async def test_job_survives_executor_close_and_the_next_executor_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANDBOX_ENABLED", "false")
    jobs = SessionJobs(max_jobs=4)
    first = _executor(tmp_path, jobs)
    try:
        started = await first.execute(
            call=ToolCall(
                call_id="1",
                name="run_command",
                args={
                    "argv": [
                        "python3",
                        "-c",
                        "print('hello-bg', flush=True); import time; time.sleep(30)",
                    ],
                    "background": True,
                    "timeout": 60,
                },
            ),
            ctx=_ctx(tmp_path),
        )
        assert started.error is None
        body = json.loads(started.blocks[0].text)  # type: ignore[index]
        assert first.running_background_jobs() == []
        assert await first.aclose() == []
        assert jobs.running()

        second = _executor(tmp_path, jobs)
        waited = await second.execute(
            call=ToolCall(
                call_id="2",
                name="await_command",
                args={"job_id": body["job_id"], "wait_seconds": 2},
            ),
            ctx=_ctx(tmp_path),
        )
        assert waited.error is None
        done = json.loads(waited.blocks[0].text)  # type: ignore[index]
        assert done["status"] == "running"
        assert "hello-bg" in done["new_output"]
        assert await second.aclose() == []
        assert jobs.running()
    finally:
        await jobs.aclose("session closed")


@pytest.mark.asyncio
async def test_cap_is_per_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_ENABLED", "false")
    monkeypatch.setenv("SANDBOX_MAX_BACKGROUND_JOBS", "1")
    jobs = SessionJobs(max_jobs=1)
    first = _executor(tmp_path, jobs)
    try:
        started = await first.execute(
            call=ToolCall(
                call_id="1",
                name="run_command",
                args={
                    "argv": ["python3", "-c", "import time; time.sleep(30)"],
                    "background": True,
                    "timeout": 60,
                },
            ),
            ctx=_ctx(tmp_path),
        )
        assert started.error is None
        second = _executor(tmp_path, jobs)
        blocked = await second.execute(
            call=ToolCall(
                call_id="2",
                name="run_command",
                args={
                    "argv": ["python3", "-c", "import time; time.sleep(30)"],
                    "background": True,
                    "timeout": 60,
                },
            ),
            ctx=_ctx(tmp_path),
        )
        assert blocked.error is not None
        assert "too many background jobs" in blocked.error
    finally:
        await jobs.aclose("session closed")


@pytest.mark.asyncio
async def test_finished_unawaited_job_wakes_once(tmp_path: Path) -> None:
    wakes: list[list[tuple[str, str]]] = []

    async def on_wake(batch: list[tuple[str, str]]) -> bool:
        wakes.append(list(batch))
        return True

    jobs = SessionJobs(max_jobs=2, on_wake=on_wake, wake_debounce_s=0)
    executor = TerminalExecutor(max_job_seconds=10, jobs=jobs)
    job = await executor.start_background(
        "python3",
        ["-c", "print('done', flush=True)"],
        log_dir=tmp_path,
    )
    await _wait_for(lambda: len(wakes) == 1)
    assert wakes[0][0][0] == job.job_id
    assert wakes[0][0][1] == "exited"
    await jobs.wake_if_idle()
    assert len(wakes) == 1
    assert await executor.aclose() == []


@pytest.mark.asyncio
async def test_awaited_job_does_not_wake(tmp_path: Path) -> None:
    wakes: list[list[tuple[str, str]]] = []

    async def on_wake(batch: list[tuple[str, str]]) -> bool:
        wakes.append(list(batch))
        return True

    jobs = SessionJobs(max_jobs=2, on_wake=on_wake, wake_debounce_s=0)
    executor = TerminalExecutor(max_job_seconds=10, jobs=jobs)
    job = await executor.start_background(
        "python3",
        ["-c", "import time; time.sleep(0.3)"],
        log_dir=tmp_path,
    )
    jobs.mark_awaited(job.job_id)
    await jobs.wait_until_settled(job.job_id, timeout=2)
    await asyncio.sleep(0.05)
    assert wakes == []
    assert jobs.snapshot_text() == ""
    assert await executor.aclose() == []


@pytest.mark.asyncio
async def test_session_aclose_kills_running_jobs(tmp_path: Path) -> None:
    jobs = SessionJobs(max_jobs=2)
    executor = TerminalExecutor(max_job_seconds=30, jobs=jobs)
    await executor.start_background(
        "python3",
        ["-c", "import time; time.sleep(30)"],
        log_dir=tmp_path,
    )
    assert await executor.aclose() == []
    killed = await jobs.aclose("session closed")
    assert len(killed) == 1
    assert killed[0].status in {JobStatus.KILLED, JobStatus.LOST}
    assert await jobs.aclose("session closed") == []


@pytest.mark.asyncio
async def test_shared_wake_budget_stops_at_three(tmp_path: Path) -> None:
    budget = WakeBudget()
    wakes: list[list[tuple[str, str]]] = []

    async def on_wake(batch: list[tuple[str, str]]) -> bool:
        wakes.append(list(batch))
        return True

    async def _inline(**kwargs: object) -> dict[str, object]:
        return {
            "ok": True,
            "exit_reason": "completed",
            "errors": [],
            "final_message": "done",
            "run_id": kwargs["run_id"],
            "child_thread_id": kwargs["child_thread_id"],
            "subagent_type": None,
        }

    supervisor = SubagentSupervisor(
        session_id="sess",
        run_store=None,
        max_background=4,
        run_inline=_inline,
        on_wake=on_wake,
        wake_budget=budget,
        wake_debounce_s=0,
    )
    for index in range(3):
        await supervisor.spawn(_spec(f"run-{index}"))
        await _wait_for(lambda i=index: len(wakes) == i + 1)

    jobs = SessionJobs(max_jobs=2, on_wake=on_wake, wake_budget=budget, wake_debounce_s=0)
    executor = TerminalExecutor(max_job_seconds=10, jobs=jobs)
    await executor.start_background(
        "python3",
        ["-c", "print('later', flush=True)"],
        log_dir=tmp_path,
    )
    await _wait_for(lambda: not jobs.running())
    await asyncio.sleep(0.05)
    assert len(wakes) == 3
    budget.note_human_turn()
    await jobs.wake_if_idle()
    assert len(wakes) == 4
    await supervisor.aclose("session closed")
