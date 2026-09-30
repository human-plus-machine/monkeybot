"""In-process background command jobs shared by the terminal and sandbox executors.

A job is a command that returns a handle immediately. Output is appended to a
log file; tool results carry only a tail plus a byte cursor into that file.
The registry caps how many jobs one executor may run at once.
"""

from __future__ import annotations

import asyncio
import codecs
import os
import tempfile
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

DEFAULT_MAX_BACKGROUND_JOBS = 4
DEFAULT_MAX_JOB_SECONDS = 3300
DEFAULT_RENEW_INTERVAL_SECONDS = 300
DEFAULT_AWAIT_WAIT_SECONDS = 300
DEFAULT_AWAIT_MAX_WAIT_SECONDS = 600
# One await_command result never carries more than this many log bytes; the
# model pages the rest by passing the returned cursor back in.
OUTPUT_CHUNK_BYTES = 16 * 1024


class JobStatus(StrEnum):
    RUNNING = "running"
    EXITED = "exited"
    KILLED = "killed"
    LOST = "lost"


class JobNotFoundError(KeyError):
    """No background job is registered under this id."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(job_id)


class JobCapExceededError(RuntimeError):
    """The executor is already at its concurrent background-job cap."""

    def __init__(self, max_jobs: int) -> None:
        self.max_jobs = max_jobs
        super().__init__(
            f"too many background jobs ({max_jobs} already running); "
            "await or kill one before starting another"
        )


@dataclass
class BackgroundJob:
    """One detached command owned by a single executor."""

    job_id: str
    command: str
    log_path: Path
    started_at: float
    timeout_seconds: int
    status: JobStatus = JobStatus.RUNNING
    exit_code: int | None = None
    error: str | None = None
    remote_id: str | None = None
    remote_cursor: int | None = None
    hit_ceiling: bool = False


class JobRegistry:
    """Per-executor job table. Not safe to share across executors."""

    def __init__(self, *, max_jobs: int) -> None:
        self.max_jobs = max(1, max_jobs)
        self._jobs: dict[str, BackgroundJob] = {}

    def running(self) -> list[BackgroundJob]:
        return [job for job in self._jobs.values() if job.status is JobStatus.RUNNING]

    def add(self, job: BackgroundJob) -> BackgroundJob:
        if len(self.running()) >= self.max_jobs:
            raise JobCapExceededError(self.max_jobs)
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> BackgroundJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    def __contains__(self, job_id: object) -> bool:
        return isinstance(job_id, str) and job_id in self._jobs


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def clamp_job_timeout(requested: int, *, ceiling: int) -> tuple[int, bool]:
    """Return ``(effective_timeout, hit_ceiling)``.

    ``hit_ceiling`` is true when the caller's timeout is at or above the
    configured job ceiling, so a kill at that limit is the platform max
    rather than the caller's own shorter deadline.
    """
    ceiling = max(1, ceiling)
    requested = max(1, requested)
    if requested >= ceiling:
        return ceiling, True
    return requested, False


def timeout_failure_message(*, timeout_seconds: int, hit_ceiling: bool) -> str:
    if hit_ceiling:
        return f"Command exceeded the {timeout_seconds}s maximum job time"
    return f"Command exceeded {timeout_seconds}s timeout"


def append_log(path: Path, text: str) -> None:
    if not text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def read_log_from(
    path: Path,
    cursor: int,
    *,
    max_bytes: int = OUTPUT_CHUNK_BYTES,
    final: bool = False,
) -> tuple[str, int]:
    """Return ``(new_text, new_cursor)`` for up to ``max_bytes`` after ``cursor``.

    Reads only the requested window, so large logs are never loaded whole.
    The returned cursor stops before a trailing partial UTF-8 sequence so the
    next read decodes it intact; ``final`` (the writer is done) decodes it as
    ``\ufffd`` instead, but only once the window reaches end of file.
    """
    cursor = max(0, cursor)
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return "", cursor
    with handle:
        size = os.fstat(handle.fileno()).st_size
        cursor = min(cursor, size)
        handle.seek(cursor)
        data = handle.read(max_bytes)
    end = cursor + len(data)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(data, final=final and end >= size)
    pending = len(decoder.getstate()[0])
    return text, end - pending


def allocate_log_path(log_dir: Path, job_id: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{job_id}.log"


def allocate_background_job(
    registry: JobRegistry,
    command: str,
    args: Sequence[str],
    *,
    effective: int,
    hit_ceiling: bool,
    log_dir: Path | None,
) -> BackgroundJob:
    """Register a new running job with its log file path. Raises when at the cap."""
    job_id = new_job_id()
    directory = log_dir if log_dir is not None else Path(tempfile.mkdtemp(prefix="monkeybot-job-"))
    job = BackgroundJob(
        job_id=job_id,
        command=" ".join([command, *args]),
        log_path=allocate_log_path(directory, job_id),
        started_at=time.monotonic(),
        timeout_seconds=effective,
        hit_ceiling=hit_ceiling,
    )
    return registry.add(job)


def format_running_jobs(jobs: Sequence[Any]) -> str:
    parts: list[str] = []
    for job in jobs:
        if job.command:
            parts.append(f"{job.job_id} ({job.command})")
        else:
            parts.append(job.job_id)
    return ", ".join(parts)


def jobs_still_running_note(jobs: Sequence[Any]) -> str:
    """Harness note injected when the model tries to finish with jobs running."""
    count = len(jobs)
    noun = "job" if count == 1 else "jobs"
    return (
        f"[Harness] {count} background {noun} still running "
        f"({format_running_jobs(jobs)}). Await them with await_command or stop "
        "them with kill_command before finishing. Do not tell the user the work "
        "is only started and end the turn; running jobs are killed when the turn ends."
    )


def jobs_check_failed_error() -> str:
    """Turn-end error when the harness cannot tell whether jobs are still running."""
    return "Could not check background jobs. Ending the turn kills any that are still running."


def jobs_will_be_killed_error(jobs: Sequence[Any]) -> str:
    count = len(jobs)
    noun = "job" if count == 1 else "jobs"
    return (
        f"{count} background {noun} still running ({format_running_jobs(jobs)}). "
        "Ending the turn kills them with no result."
    )


def jobs_killed_at_turn_end_error(jobs: Sequence[Any]) -> str:
    count = len(jobs)
    noun = "job" if count == 1 else "jobs"
    return f"Killed {count} background {noun} at turn end: {format_running_jobs(jobs)}."


async def poll_background_job(
    executor: Any,
    job_id: str,
    *,
    wait_seconds: float,
    cursor: int,
) -> dict[str, Any]:
    """Block until ``job_id`` leaves ``running`` or ``wait_seconds`` elapses.

    Uses ``wait_until_settled`` when the executor has one (local subprocesses)
    and otherwise polls ``get_status`` every two seconds (remote sandboxes).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, wait_seconds)
    while True:
        job = await executor.get_status(job_id)
        if job.status is not JobStatus.RUNNING:
            break
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        waiter = getattr(executor, "wait_until_settled", None)
        slice_s = min(2.0, remaining)
        if waiter is not None:
            await waiter(job_id, timeout=slice_s)
        else:
            await asyncio.sleep(slice_s)
    job = await executor.get_status(job_id)
    chunk, new_cursor = await executor.read_output(job_id, max(0, cursor))
    ok = job.status is JobStatus.RUNNING or (
        job.status is JobStatus.EXITED and (job.exit_code or 0) == 0
    )
    payload: dict[str, Any] = {
        "ok": ok,
        "status": job.status.value,
        "exit_code": job.exit_code,
        "new_output": chunk,
        "cursor": new_cursor,
        "has_more": log_size(job.log_path) > new_cursor,
        "log_path": str(job.log_path),
        "job_id": job.job_id,
    }
    if job.error:
        payload["error"] = job.error
    return payload
