"""Background subagent supervisor."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.subagents.subagent_proto import SubagentEnvelope
from monkeybot.core.subagents.supervisor import (
    BackgroundCapError,
    SubagentSpawn,
    SubagentSupervisor,
)
from testkit.context import tool_ctx


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


@pytest.mark.asyncio
async def test_cap_and_stop_unblocks_await_without_killing() -> None:
    started = asyncio.Event()

    async def _inline(**kwargs: object) -> dict[str, object]:
        started.set()
        cancelled = kwargs["cancelled"]
        assert isinstance(cancelled, asyncio.Event)
        await cancelled.wait()
        return {
            "ok": False,
            "exit_reason": kwargs["cancel_exit_reason"],
            "errors": [kwargs["cancel_message"]],
            "final_message": "",
            "run_id": kwargs["run_id"],
            "child_thread_id": kwargs["child_thread_id"],
            "subagent_type": None,
        }

    supervisor = SubagentSupervisor(
        session_id="sess", run_store=None, max_background=1, run_inline=_inline
    )
    await supervisor.spawn(_spec("run-1"))
    await asyncio.wait_for(started.wait(), timeout=1)
    with pytest.raises(BackgroundCapError):
        await supervisor.spawn(_spec("run-2"))

    turn_cancel = asyncio.Event()
    turn_cancel.set()
    waiting = await supervisor.await_run(
        "run-1", session_id="sess", wait_seconds=5, turn_cancelled=turn_cancel
    )
    assert waiting is not None
    assert waiting["status"] == "running"
    assert supervisor.running_count() == 1

    note = supervisor.snapshot_text()
    assert "Answer the user." in note
    assert "Do not call await_task for a child that is still running" in note
    assert "Call await_task before reporting results" not in note

    killed = await supervisor.kill("run-1", session_id="sess")
    assert killed is not None
    assert killed["status"] == "failed"
    assert killed["exit_reason"] == "killed"
    await supervisor.aclose("test")


@pytest.mark.asyncio
async def test_finished_snapshot_still_requires_await_before_reporting() -> None:
    release = asyncio.Event()

    async def _inline(**kwargs: object) -> dict[str, object]:
        await release.wait()
        return {
            "ok": True,
            "exit_reason": "completed",
            "final_message": "done",
            "run_id": kwargs["run_id"],
            "child_thread_id": kwargs["child_thread_id"],
            "subagent_type": None,
        }

    supervisor = SubagentSupervisor(
        session_id="sess", run_store=None, max_background=1, run_inline=_inline
    )
    await supervisor.spawn(_spec("run-done"))
    release.set()
    task = supervisor._runs["run-done"].task
    assert task is not None
    await asyncio.wait_for(task, timeout=1)

    note = supervisor.snapshot_text()
    assert "finished (completed). Call await_task before reporting." in note
    assert note.endswith("Call await_task before reporting. Do not spin-poll.")
    assert "Answer the user." not in note
    await supervisor.aclose("test")


@pytest.mark.asyncio
async def test_snapshot_lists_every_running_child_and_caps_finished() -> None:
    async def _inline(**kwargs: object) -> dict[str, object]:
        run_id = str(kwargs["run_id"])
        if run_id.startswith("fin"):
            return {
                "ok": True,
                "exit_reason": "completed",
                "final_message": "done",
                "run_id": run_id,
                "child_thread_id": kwargs["child_thread_id"],
                "subagent_type": None,
            }
        cancelled = kwargs["cancelled"]
        assert isinstance(cancelled, asyncio.Event)
        await cancelled.wait()
        return {
            "ok": False,
            "exit_reason": kwargs["cancel_exit_reason"],
            "errors": [kwargs["cancel_message"]],
            "final_message": "",
            "run_id": run_id,
            "child_thread_id": kwargs["child_thread_id"],
            "subagent_type": None,
        }

    supervisor = SubagentSupervisor(
        session_id="sess", run_store=None, max_background=4, run_inline=_inline
    )
    for index in range(20):
        run_id = f"fin-{index}"
        await supervisor.spawn(_spec(run_id))
        task = supervisor._runs[run_id].task
        assert task is not None
        await asyncio.wait_for(task, timeout=1)
    for index in range(4):
        await supervisor.spawn(_spec(f"run-{index}"))

    note = supervisor.snapshot_text()
    assert note.count(" finished (") == 8
    assert note.count(" running (") == 4
    assert "(+12 more finished; listed once those above are awaited)" in note
    assert "Answer the user." in note
    await supervisor.aclose("test")
