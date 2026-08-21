"""Process-group helpers for killing subprocess trees on timeout/cancel."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys

SUPPORTS_PROCESS_GROUPS = sys.platform != "win32"


def process_group_id(pid: int | None) -> int | None:
    if pid is None or not SUPPORTS_PROCESS_GROUPS:
        return None
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return pid
    except OSError:
        return None


def kill_process_group(
    pgid: int | None,
    proc: asyncio.subprocess.Process | None = None,
) -> None:
    """SIGKILL ``pgid`` when known so pipe-holding descendants die too."""
    if pgid is not None and SUPPORTS_PROCESS_GROUPS:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if proc is not None and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def stop_subagent_process(
    proc: asyncio.subprocess.Process | None,
    *,
    pgid: int | None = None,
) -> None:
    """SIGTERM/SIGKILL the process group (or direct child) on timeout/cancel."""
    if proc is None and pgid is None:
        return
    try:
        if pgid is not None and SUPPORTS_PROCESS_GROUPS:
            os.killpg(pgid, signal.SIGTERM)
        elif proc is not None and proc.returncode is None:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    if proc is not None and proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=8.0)
            return
        except TimeoutError:
            pass
    else:
        await asyncio.sleep(0.05)
    kill_process_group(pgid, proc)
    if proc is not None and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
