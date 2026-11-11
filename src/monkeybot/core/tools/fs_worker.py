"""Run workspace file tools off the asyncio event loop.

One loop serves every session in the process, so a grep or a batch of reads
that blocks it stalls all of them. The work here is disk and child processes,
which release the GIL while they wait, so a thread pool gets it off the loop.

The pool is separate from ``asyncio.to_thread``'s default executor. Provider
streams and memory also use that one, and a batch of slow greps must not be
able to fill it.

There is no size setting. The pool is created on first use at
``min(32, effective_cpus + 4)`` — the stdlib's formula for an I/O pool — from
the CPUs this process may actually run on. That is the affinity mask, further
limited by a cgroup quota when the process is in a container. Python does not
read the quota itself, so without that check a 1-CPU container on a large host
would start 32 threads. The size is fixed for the life of the process; the
gateway, the CLI, and each subagent worker size their own.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import os
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

from monkeybot.core.logging_utils import kv

logger = logging.getLogger(__name__)

_POOL_CAP = 32
_POOL_EXTRA = 4
_CGROUP_V2_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
_CGROUP_V1_QUOTA = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
_CGROUP_V1_PERIOD = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")

_pool: ThreadPoolExecutor | None = None
_pool_guard = threading.Lock()

_T = TypeVar("_T")


class FsCancelledError(Exception):
    """The file-tool call was cancelled.

    Not a failed scan and not an ``OSError``: a cancelled ripgrep exits
    non-zero, and treating that as a broken ``rg`` would start the Python
    walker in a thread the caller has already abandoned.
    """


class FsCancel:
    """Stop one file-tool call running on a worker thread.

    A thread cannot be cancelled from outside. The worker checks
    :meth:`cancelled` between files and directories, and any child process
    attached here is killed so a blocked ``communicate`` returns.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._procs: list[subprocess.Popen[str]] = []

    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def attach(self, proc: subprocess.Popen[str]) -> None:
        """Register ``proc``. If the call is already cancelled, kill it now."""
        with self._lock:
            if self._cancelled:
                _kill(proc)
                return
            self._procs.append(proc)

    def detach(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            try:
                self._procs.remove(proc)
            except ValueError:
                return

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            procs = list(self._procs)
        for proc in procs:
            _kill(proc)


_current_cancel: contextvars.ContextVar[FsCancel | None] = contextvars.ContextVar(
    "monkeybot_fs_cancel", default=None
)


def current_cancel() -> FsCancel | None:
    """The cancel handle for the file-tool call running on this thread, if any."""
    return _current_cancel.get()


def _kill(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.kill()
    except OSError:
        return


def _affinity_cpus() -> int:
    """CPUs the process is allowed to run on. At least 1."""
    process_cpu_count = getattr(os, "process_cpu_count", None)
    if callable(process_cpu_count):
        count = process_cpu_count()
        if isinstance(count, int) and count > 0:
            return count
    sched = getattr(os, "sched_getaffinity", None)
    if callable(sched):
        try:
            count = len(sched(0))
        except OSError:
            count = 0
        if count > 0:
            return count
    count = os.cpu_count()
    if isinstance(count, int) and count > 0:
        return count
    return 1


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _ceil_cpus(quota: str, period: str) -> int | None:
    try:
        q = int(quota)
        p = int(period)
    except ValueError:
        return None
    if q < 0 or p <= 0:
        return None
    return max(1, math.ceil(q / p))


def _cgroup_cpu_quota() -> int | None:
    """Whole CPUs the cgroup allows, or ``None`` when there is no quota.

    v2 ``cpu.max`` wins when the file is present: ``max`` means unlimited.
    Otherwise v1 ``cpu.cfs_quota_us`` / ``cpu.cfs_period_us``, where ``-1``
    means unlimited. Missing or unreadable files mean no quota.
    """
    v2 = _read_text(_CGROUP_V2_CPU_MAX)
    if v2 is not None:
        parts = v2.split()
        if len(parts) >= 2 and parts[0] == "max":
            return None
        if len(parts) >= 2:
            parsed = _ceil_cpus(parts[0], parts[1])
            if parsed is not None:
                return parsed
        return None
    quota = _read_text(_CGROUP_V1_QUOTA)
    period = _read_text(_CGROUP_V1_PERIOD)
    if quota is None or period is None or quota == "-1":
        return None
    return _ceil_cpus(quota, period)


def _cpu_limits() -> tuple[int, int | None, int]:
    """``(affinity, cgroup quota or None, effective cpus)``. Effective is at least 1."""
    affinity = max(1, _affinity_cpus())
    quota = _cgroup_cpu_quota()
    if quota is None:
        return affinity, None, affinity
    return affinity, quota, max(1, min(affinity, quota))


def effective_cpus() -> int:
    """CPUs this process may use: affinity, capped by a cgroup quota when set."""
    return _cpu_limits()[2]


def pool_size(cpus: int | None = None) -> int:
    """Worker threads for ``cpus`` (or this process, when ``cpus`` is omitted)."""
    n = effective_cpus() if cpus is None else max(1, cpus)
    return min(_POOL_CAP, n + _POOL_EXTRA)


def fs_pool() -> ThreadPoolExecutor:
    """The process-wide file-tool pool, created on first use and never shut down."""
    global _pool
    with _pool_guard:
        if _pool is None:
            affinity, quota, cpus = _cpu_limits()
            size = pool_size(cpus)
            logger.info(
                "fs worker pool %s",
                kv(
                    size=size,
                    affinity_cpus=affinity,
                    cgroup_quota=quota if quota is not None else "none",
                ),
            )
            _pool = ThreadPoolExecutor(max_workers=size, thread_name_prefix="fs-worker")
        return _pool


async def run_fs(
    fn: Callable[..., _T],
    /,
    *args: object,
    cancel: FsCancel | None = None,
    **kwargs: object,
) -> _T:
    """Run ``fn`` on the file-tool pool and copy this task's context into it.

    ``cancel``, when given, is visible to ``fn`` through :func:`current_cancel`.
    Cancelling the awaiting task calls ``cancel.cancel()`` (which kills any
    attached child) and re-raises. The thread then stops at its next check.
    """
    ctx = contextvars.copy_context()

    def _call() -> _T:
        if cancel is not None:
            _current_cancel.set(cancel)
        return fn(*args, **kwargs)

    try:
        return await asyncio.get_running_loop().run_in_executor(fs_pool(), ctx.run, _call)
    except asyncio.CancelledError:
        if cancel is not None:
            cancel.cancel()
        raise
