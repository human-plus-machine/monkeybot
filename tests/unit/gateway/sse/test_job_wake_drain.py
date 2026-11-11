"""A harness follow-up for a background command is not dropped for lack of run ids."""

from __future__ import annotations

import pytest

from monkeybot.core.runtime.input_admission import InputAdmission
from monkeybot.core.types.content_blocks import Text
from monkeybot.gateway.sse.routes import _drain_follow_up


class _Jobs:
    def __init__(self, needed: bool) -> None:
        self.needed = needed

    def jobs_still_need_wake(self, job_ids: tuple[str, ...]) -> bool:
        del job_ids
        return self.needed

    async def wake_if_idle(self) -> None:
        return None


class _Supervisor:
    def __init__(self, needed_ids: set[str]) -> None:
        self.needed_ids = needed_ids

    def runs_still_need_wake(self, run_ids: tuple[str, ...]) -> bool:
        return any(run_id in self.needed_ids for run_id in run_ids)

    async def wake_if_idle(self) -> None:
        return None


class _Loop:
    def __init__(self) -> None:
        self.started: list[str] = []

    async def start_turn(
        self,
        session_id: str,
        request_id: str,
        user_content: list[Text],
        *,
        provenance: str = "human",
    ) -> None:
        del session_id, user_content, provenance
        self.started.append(request_id)


class _Bus:
    def __init__(self, jobs: _Jobs, supervisor: _Supervisor) -> None:
        self.current_request_id: str | None = None
        self.admission = InputAdmission()
        self.session_jobs = jobs
        self.subagent_supervisor = supervisor
        self.active_turn_task = None

    def cancel_follow_up_retry(self) -> None:
        return None


async def _drain(bus: _Bus, loop: _Loop) -> None:
    await _drain_follow_up(bus=bus, loop_ref=loop, storage=None, session_id="s")
    task = bus.active_turn_task
    if task is not None:
        await task


@pytest.mark.asyncio
async def test_job_only_wake_runs_until_the_job_is_awaited() -> None:
    jobs = _Jobs(needed=True)
    bus = _Bus(jobs, _Supervisor(set()))
    loop = _Loop()
    bus.admission.enqueue_follow_up(
        "wake-job",
        [Text(text="command finished")],
        provenance="harness",
        harness_job_ids=("job-1",),
    )
    await _drain(bus, loop)
    assert loop.started == ["wake-job"]

    jobs.needed = False
    loop.started.clear()
    bus.admission.enqueue_follow_up(
        "wake-job-2",
        [Text(text="command finished")],
        provenance="harness",
        harness_job_ids=("job-1",),
    )
    await _drain(bus, loop)
    assert loop.started == []


@pytest.mark.asyncio
async def test_run_wake_still_runs_when_there_are_no_job_ids() -> None:
    bus = _Bus(_Jobs(needed=False), _Supervisor({"run-1"}))
    loop = _Loop()
    bus.admission.enqueue_follow_up(
        "wake-run",
        [Text(text="task finished")],
        provenance="harness",
        harness_run_ids=("run-1",),
    )
    await _drain(bus, loop)
    assert loop.started == ["wake-run"]

    loop.started.clear()
    bus.admission.enqueue_follow_up(
        "wake-run-done",
        [Text(text="task finished")],
        provenance="harness",
        harness_run_ids=("run-1",),
    )
    bus.subagent_supervisor.needed_ids.clear()
    await _drain(bus, loop)
    assert loop.started == []
