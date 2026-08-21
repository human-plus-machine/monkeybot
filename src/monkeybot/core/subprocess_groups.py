"""Process-group helpers for killing subprocess trees on timeout/cancel."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

SUPPORTS_PROCESS_GROUPS = sys.platform != "win32"


def process_group_id(pid: int | None) -> int | None:
    if pid is None or not SUPPORTS_PROCESS_GROUPS:
        return None
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return None
    except OSError:
        return None


def _direct_child_pids(pid: int) -> list[int]:
    """Return immediate child PIDs of ``pid`` (best effort)."""
    if pid <= 0:
        return []
    children_path = Path(f"/proc/{pid}/task/{pid}/children")
    if children_path.exists():
        try:
            raw = children_path.read_text(encoding="utf-8").strip()
            if not raw:
                return []
            return [int(token) for token in raw.split()]
        except (OSError, ValueError):
            return []
    try:
        completed = subprocess.run(
            ["ps", "-o", "pid=", "-P", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    out: list[int] = []
    for line in (completed.stdout or "").splitlines():
        token = line.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError:
            continue
    return out


def iter_process_tree(root_pid: int) -> list[int]:
    """Return ``root_pid`` and descendant PIDs, parents before children."""
    if root_pid <= 0:
        return []
    ordered: list[int] = []
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        ordered.append(pid)
        for child in _direct_child_pids(pid):
            if child not in seen:
                stack.append(child)
    return ordered


def _signal_pid_or_group(pid: int, sig: int) -> None:
    pgid: int | None = None
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    except OSError:
        pgid = None
    if pgid is not None and SUPPORTS_PROCESS_GROUPS:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, sig)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, sig)


def signal_process_tree(root_pid: int, sig: int) -> None:
    """Signal each live process in the tree, deepest first (each in its own pg)."""
    for pid in reversed(iter_process_tree(root_pid)):
        _signal_pid_or_group(pid, sig)


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
    """SIGTERM/SIGKILL the subagent tree (or direct child) on timeout/cancel."""
    if proc is None and pgid is None:
        return
    root_pid = proc.pid if proc is not None else None
    try:
        if root_pid is not None and SUPPORTS_PROCESS_GROUPS:
            signal_process_tree(root_pid, signal.SIGTERM)
        elif pgid is not None and SUPPORTS_PROCESS_GROUPS:
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
    if root_pid is not None and SUPPORTS_PROCESS_GROUPS:
        signal_process_tree(root_pid, signal.SIGKILL)
    else:
        kill_process_group(pgid, proc)
    if proc is not None and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
