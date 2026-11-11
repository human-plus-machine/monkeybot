"""Session-owned local background commands.

A turn builds a ``TerminalExecutor`` to check policy, then hands the spawned
process to this table. The table outlives that turn. ``await_command`` and
``kill_command`` on a later turn use the same table. Closing the session kills
whatever is still running.

A private table (no ``on_wake``) is what a turn-scoped executor creates for
itself. ``aclose`` on that executor kills its jobs, which is the Lambda, voice,
and sandbox-host behaviour.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from monkeybot.core.runtime.wake_budget import WakeBudget
from monkeybot.core.subprocess_groups import (
    SUPPORTS_PROCESS_GROUPS,
    kill_process_group,
    process_group_id,
)
from monkeybot.core.tools.background_jobs import (
    BackgroundJob,
    JobCapExceededError,
    JobRegistry,
    JobStatus,
    allocate_background_job,
    append_log,
    read_log_from,
    timeout_failure_message,
)

logger = logging.getLogger(__name__)

# After the child exits or is killed, wait at most this long for the watcher
# to close the log. Descendants that keep the pipe open must not block kill.
STREAM_DRAIN_TIMEOUT_SEC = 2.0
_WAKE_DEBOUNCE_S = 2.0
_MAX_FINISHED_LINES = 8

WakeCallback = Callable[[list[tuple[str, str]]], Awaitable[bool]]


async def wait_for_exit(process: asyncio.subprocess.Process, timeout: float) -> bool:
    """Wait until the direct child exits.

    Returns True if still running after ``timeout``.

    Polls ``returncode`` instead of awaiting ``process.wait()`` alone: when a
    descendant keeps stdout/stderr open, asyncio can set ``returncode`` while
    ``wait()`` never completes.
    """
    if process.returncode is not None:
        return False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    wait_task = asyncio.create_task(process.wait())
    try:
        while process.returncode is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return True
            await asyncio.wait({wait_task}, timeout=min(remaining, 0.1))
            if wait_task.done() and process.returncode is None:
                await wait_task
        return False
    finally:
        if not wait_task.done():
            wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wait_task


@dataclass
class _LocalProcess:
    """Host subprocess backing one background job."""

    process: asyncio.subprocess.Process
    pgid: int | None
    handle: object
    done: asyncio.Event
    kill_requested: bool = False
    watcher: asyncio.Task[None] | None = None


@dataclass
class _JobMeta:
    awaited: bool = False
    woken: bool = False


@dataclass
class SessionJobs:
    """One per session on the SSE gateway, or one private table per executor."""

    max_jobs: int
    on_wake: WakeCallback | None = None
    wake_budget: WakeBudget | None = None
    wake_debounce_s: float = _WAKE_DEBOUNCE_S
    _registry: JobRegistry = field(init=False)
    _procs: dict[str, _LocalProcess] = field(init=False)
    _meta: dict[str, _JobMeta] = field(init=False)
    _budget: WakeBudget = field(init=False)
    _wake_task: asyncio.Task[None] | None = field(init=False, default=None)
    _closed: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self._registry = JobRegistry(max_jobs=max(1, self.max_jobs))
        self._procs = {}
        self._meta = {}
        self._budget = self.wake_budget if self.wake_budget is not None else WakeBudget()

    def running(self) -> list[BackgroundJob]:
        return self._registry.running()

    def has_job(self, job_id: str) -> bool:
        return job_id in self._registry

    def set_max_jobs(self, max_jobs: int) -> None:
        """Apply a new cap. A running job keeps the cap it started under."""
        if self.running():
            return
        self._registry.max_jobs = max(1, max_jobs)
        self.max_jobs = self._registry.max_jobs

    def note_human_turn(self) -> None:
        self._budget.note_human_turn()

    def mark_awaited(self, job_id: str) -> None:
        meta = self._meta.get(job_id)
        if meta is not None:
            meta.awaited = True

    def snapshot_lines(self) -> list[str]:
        """Lines for the volatile prompt note. Empty when nothing is outstanding."""
        lines: list[str] = []
        finished_shown = 0
        finished_hidden = 0
        for job in self._registry.all():
            meta = self._meta.get(job.job_id)
            if meta is not None and meta.awaited:
                continue
            if job.status is JobStatus.RUNNING:
                lines.append(f"- {job.job_id} running ({job.command}).")
                continue
            if finished_shown >= _MAX_FINISHED_LINES:
                finished_hidden += 1
                continue
            finished_shown += 1
            lines.append(
                f"- {job.job_id} finished ({job.status.value}: {job.command}). "
                "Call await_command before reporting."
            )
        if finished_hidden:
            lines.append(
                f"- (+{finished_hidden} more finished; listed once those above are awaited)"
            )
        return lines

    def snapshot_text(self) -> str:
        lines = self.snapshot_lines()
        if not lines:
            return ""
        running = any(job.status is JobStatus.RUNNING for job in self.running())
        if running:
            footer = (
                "Answer the user. Do not call await_command for a command that is still "
                "running unless they asked for that result. Call await_command before "
                "reporting a command that has finished."
            )
        else:
            footer = "Call await_command before reporting. Do not spin-poll."
        return "\n".join(lines) + "\n" + footer

    def jobs_still_need_wake(self, job_ids: tuple[str, ...]) -> bool:
        for job_id in job_ids:
            meta = self._meta.get(job_id)
            if meta is None or not meta.awaited:
                return True
        return False

    def pending_wake(self) -> list[tuple[str, str]]:
        """Terminal jobs not yet awaited and not yet woken."""
        out: list[tuple[str, str]] = []
        for job in self._registry.all():
            meta = self._meta.get(job.job_id)
            if meta is None or job.status is JobStatus.RUNNING or meta.awaited or meta.woken:
                continue
            out.append((job.job_id, job.status.value))
        return out

    async def spawn(
        self,
        command: str,
        args: Sequence[str],
        *,
        effective: int,
        hit_ceiling: bool,
        log_dir: Path | None,
        executable: str,
        exec_args: list[str],
        exec_cwd: str | None,
        env: dict[str, str] | None,
    ) -> BackgroundJob:
        """Register and start a process. Policy checks belong to the caller."""
        if self._closed:
            raise JobCapExceededError(self._registry.max_jobs)
        job = allocate_background_job(
            self._registry,
            command,
            args,
            effective=effective,
            hit_ceiling=hit_ceiling,
            log_dir=log_dir,
        )
        self._meta[job.job_id] = _JobMeta()
        try:
            await self._spawn_local(job, executable, exec_args, exec_cwd, env)
        except Exception:
            # The caller sees the start error. Do not also wake the session.
            self.mark_awaited(job.job_id)
            raise
        return job

    async def get_status(self, job_id: str) -> BackgroundJob:
        return self._registry.get(job_id)

    async def read_output(self, job_id: str, cursor: int) -> tuple[str, int]:
        job = self._registry.get(job_id)
        return read_log_from(job.log_path, cursor, final=job.status is not JobStatus.RUNNING)

    async def wait_until_settled(self, job_id: str, *, timeout: float) -> None:
        """Block until the job leaves ``running`` or ``timeout`` seconds elapse."""
        job = self._registry.get(job_id)
        if job.status is not JobStatus.RUNNING:
            return
        local = self._procs.get(job_id)
        if local is None:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(local.done.wait(), timeout=max(0.0, timeout))

    async def kill(self, job_id: str, *, reason: str | None = None) -> BackgroundJob:
        job = self._registry.get(job_id)
        if job.status is not JobStatus.RUNNING:
            return job
        local = self._procs.get(job_id)
        job.error = job.error or reason or "killed"
        if local is None:
            job.status = JobStatus.LOST
            job.error = job.error or "lost"
            logger.warning(
                "background job lost",
                extra={
                    "component": "session_jobs",
                    "job_id": job.job_id,
                    "status": job.status.value,
                },
            )
            self._note_settled(job)
            return job
        local.kill_requested = True
        kill_process_group(local.pgid, local.process)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(local.done.wait(), timeout=STREAM_DRAIN_TIMEOUT_SEC)
        if job.status is JobStatus.RUNNING:
            job.status = JobStatus.KILLED
            if job.exit_code is None:
                job.exit_code = (
                    local.process.returncode if local.process.returncode is not None else -1
                )
            logger.info(
                "background job killed",
                extra={
                    "component": "session_jobs",
                    "job_id": job.job_id,
                    "exit_code": job.exit_code,
                },
            )
            self._note_settled(job)
        return job

    async def aclose(self, reason: str) -> list[BackgroundJob]:
        """Kill jobs still running. A second call returns an empty list."""
        self._closed = True
        if self._wake_task is not None and not self._wake_task.done():
            self._wake_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._wake_task
        killed: list[BackgroundJob] = []
        for job in list(self.running()):
            try:
                killed.append(await self.kill(job.job_id, reason=reason))
            except Exception:
                logger.warning(
                    "failed to kill background job during aclose",
                    extra={"component": "session_jobs", "job_id": job.job_id},
                    exc_info=True,
                )
                if job.status is JobStatus.RUNNING:
                    job.status = JobStatus.LOST
                    job.error = job.error or "lost during shutdown"
                killed.append(job)
        if killed:
            logger.warning(
                "killed background jobs %s",
                extra={"component": "session_jobs", "reason": reason, "jobs": len(killed)},
            )
        return killed

    async def wake_if_idle(self) -> None:
        """Send a deferred wake once the session is idle.

        A completion that landed during a user turn leaves the batch pending.
        The turn end calls this after clearing ``current_request_id``.
        """
        task = self._wake_task
        if task is not None and not task.done():
            return
        try:
            await self._emit_wake()
        except Exception:
            logger.warning("background command wake failed", exc_info=True)

    async def _spawn_local(
        self,
        job: BackgroundJob,
        executable: str,
        exec_args: list[str],
        exec_cwd: str | None,
        env: dict[str, str] | None,
    ) -> None:
        handle = job.log_path.open("ab")
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *exec_args,
                stdout=handle,
                stderr=asyncio.subprocess.STDOUT,
                cwd=exec_cwd,
                env=env,
                start_new_session=SUPPORTS_PROCESS_GROUPS,
            )
        except Exception:
            handle.close()
            job.status = JobStatus.LOST
            job.error = "failed to start background command"
            logger.warning(
                "background command failed to start",
                extra={"component": "session_jobs", "job_id": job.job_id},
                exc_info=True,
            )
            raise
        local = _LocalProcess(
            process=process,
            pgid=process_group_id(process.pid),
            handle=handle,
            done=asyncio.Event(),
        )
        self._procs[job.job_id] = local
        local.watcher = asyncio.create_task(
            self._watch_local(job, local),
            name=f"bg-job-{job.job_id}",
        )

    async def _watch_local(self, job: BackgroundJob, local: _LocalProcess) -> None:
        try:
            timed_out = await wait_for_exit(local.process, job.timeout_seconds)
            if timed_out and not local.kill_requested:
                kill_process_group(local.pgid, local.process)
                with contextlib.suppress(ProcessLookupError):
                    await wait_for_exit(local.process, STREAM_DRAIN_TIMEOUT_SEC)
            exit_code = local.process.returncode
            job.exit_code = exit_code if exit_code is not None else -1
            if local.kill_requested:
                job.status = JobStatus.KILLED
                job.error = job.error or "killed"
            elif timed_out:
                job.status = JobStatus.KILLED
                job.error = timeout_failure_message(
                    timeout_seconds=job.timeout_seconds,
                    hit_ceiling=job.hit_ceiling,
                )
                append_log(job.log_path, f"\n[{job.error}]\n")
            else:
                job.status = JobStatus.EXITED
            logger.info(
                "background job %s",
                job.status.value,
                extra={
                    "component": "session_jobs",
                    "job_id": job.job_id,
                    "exit_code": job.exit_code,
                },
            )
        except Exception:
            logger.warning(
                "background job watcher failed",
                extra={"component": "session_jobs", "job_id": job.job_id},
                exc_info=True,
            )
            if job.status is JobStatus.RUNNING:
                job.status = JobStatus.LOST
                job.error = job.error or "background job lost"
        finally:
            close = getattr(local.handle, "close", None)
            if close is not None:
                close()
            self._note_settled(job)
            local.done.set()

    def _note_settled(self, job: BackgroundJob) -> None:
        meta = self._meta.get(job.job_id)
        if self._closed or meta is None or meta.awaited or job.status is JobStatus.RUNNING:
            return
        self._schedule_wake()

    def _schedule_wake(self) -> None:
        if self.on_wake is None or self._closed:
            return
        if self._wake_task is not None and not self._wake_task.done():
            return

        async def _flush() -> None:
            try:
                await asyncio.sleep(self.wake_debounce_s)
                await self._emit_wake()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("background command wake failed", exc_info=True)

        self._wake_task = asyncio.create_task(_flush(), name="session-jobs-wake")

    async def _emit_wake(self) -> None:
        batch = self.pending_wake()
        if not batch or self.on_wake is None or not self._budget.allow():
            return
        enqueued = await self.on_wake(batch)
        if not enqueued:
            return
        for job_id, _status in batch:
            meta = self._meta.get(job_id)
            if meta is not None:
                meta.woken = True
        self._budget.record()
