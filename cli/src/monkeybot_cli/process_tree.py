"""Shared helpers for spawning and tearing down process groups/trees."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from typing import Any

IS_WINDOWS = sys.platform == "win32"

# Match ``monkeybot run``: give the gateway a moment to leave the port, then
# SIGKILL the whole tree. A shim can exit on the first signal while a
# grandchild is still listening, so leader exit alone is not success.
SHUTDOWN_TIMEOUT_SECS = 5.0
KILL_TIMEOUT_SECS = 1.0


def popen_kwargs_for_platform() -> dict[str, object]:
    if IS_WINDOWS:
        # getattr fallback: this constant only exists on Windows builds of
        # `subprocess`, so a plain attribute access breaks importing/testing
        # this module on POSIX even though the branch never runs there.
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def kill_process_tree(pid: int) -> None:
    """Best-effort kill of the shell process group/tree.

    Swallow lookup/permission races — leader exit can make killpg raise
    ``PermissionError`` even though descendants still need a signal.
    """
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        except OSError:
            pass
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def signal_process_group(proc: Any, signum: int) -> None:
    """Signal the child's process group, falling back to the direct pid.

    POSIX children are started with ``start_new_session``, so ``proc.pid`` is
    the process-group id. Signaling that group reaches a shim and the gateway
    grandchild. A dead leader does not make ``killpg`` a no-op: the group
    lives until the last member exits.
    """
    pid = proc.pid
    if not isinstance(pid, int):
        return
    if IS_WINDOWS:
        try:
            proc.send_signal(signum)
        except OSError as exc:
            print(f"failed to signal gateway: {exc}", file=sys.stderr)
        return
    try:
        os.killpg(pid, signum)
        return
    except ProcessLookupError:
        return
    except OSError as exc:
        try:
            proc.send_signal(signum)
        except OSError:
            print(f"failed to signal gateway: {exc}", file=sys.stderr)


def process_group_alive(pid: int) -> bool:
    """Return True when any process is still in the POSIX group ``pid``."""
    if IS_WINDOWS:
        return False
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _leader_reaped(proc: Any) -> bool:
    poll = getattr(proc, "poll", None)
    if callable(poll):
        return poll() is not None
    return getattr(proc, "returncode", None) is not None


def _tree_is_gone(proc: Any) -> bool:
    if not _leader_reaped(proc):
        return False
    if IS_WINDOWS:
        return True
    pid = proc.pid
    if not isinstance(pid, int):
        return True
    return not process_group_alive(pid)


def _escalate(proc: Any) -> None:
    pid = proc.pid
    if not isinstance(pid, int):
        return
    print("gateway did not exit after signal; killing process tree", file=sys.stderr)
    kill_process_tree(pid)


def stop_process_group(
    proc: subprocess.Popen[Any],
    *,
    shutdown_timeout: float = SHUTDOWN_TIMEOUT_SECS,
    kill_timeout: float = KILL_TIMEOUT_SECS,
) -> None:
    """SIGTERM the process group, then SIGKILL it if anything is left."""
    if not isinstance(proc.pid, int) or _tree_is_gone(proc):
        return
    signal_process_group(proc, signal.SIGTERM)
    deadline = time.monotonic() + shutdown_timeout
    while not _tree_is_gone(proc):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _escalate(proc)
            try:
                proc.wait(timeout=kill_timeout)
            except subprocess.TimeoutExpired:
                print("gateway still running after kill", file=sys.stderr)
            return
        if proc.poll() is None:
            try:
                proc.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
        else:
            time.sleep(min(0.1, remaining))


async def stop_process_group_async(
    proc: asyncio.subprocess.Process,
    *,
    shutdown_timeout: float = SHUTDOWN_TIMEOUT_SECS,
    kill_timeout: float = KILL_TIMEOUT_SECS,
) -> None:
    """Async counterpart of :func:`stop_process_group`."""
    if not isinstance(proc.pid, int) or _tree_is_gone(proc):
        return
    signal_process_group(proc, signal.SIGTERM)
    deadline = time.monotonic() + shutdown_timeout
    while not _tree_is_gone(proc):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _escalate(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=kill_timeout)
            except TimeoutError:
                print("gateway still running after kill", file=sys.stderr)
            return
        await asyncio.sleep(min(0.1, remaining))
