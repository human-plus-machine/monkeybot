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
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
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


def _signal_group_and_tree(root_pid: int | None, pgid: int | None, sig: int) -> None:
    """Signal the known ``pgid`` directly, plus walk the live tree from ``root_pid``.

    ``root_pid`` must be a still-live leader. Once asyncio has reaped the child,
    that PID is freed and walking it can ``killpg`` an unrelated recycled
    process. The spawn-time ``pgid`` is then the only safe signal that still
    reaches descendants.
    """
    if root_pid is not None:
        signal_process_tree(root_pid, sig)
    if pgid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, sig)


async def stop_subagent_process(
    proc: asyncio.subprocess.Process | None,
    *,
    pgid: int | None = None,
) -> None:
    """SIGTERM/SIGKILL the subagent tree (or direct child) on timeout/cancel."""
    if proc is None and pgid is None:
        return
    root_pid = proc.pid if proc is not None and proc.returncode is None else None
    if SUPPORTS_PROCESS_GROUPS:
        _signal_group_and_tree(root_pid, pgid, signal.SIGTERM)
    elif proc is not None and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            proc.terminate()
    if proc is not None and proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=8.0)
            return
        except TimeoutError:
            pass
    else:
        await asyncio.sleep(0.05)
    if SUPPORTS_PROCESS_GROUPS:
        _signal_group_and_tree(root_pid, pgid, signal.SIGKILL)
    else:
        kill_process_group(pgid, proc)
    if proc is not None and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()


def process_identity(pid: int) -> str | None:
    """Stable token for ``pid``, or None if the process is gone.

    Used so a restart does not SIGKILL a reused PID. Prefer Linux ``/proc``
    starttime; fall back to ``ps`` start time on other Unix platforms.
    """
    if pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            raw = proc_stat.read_text(encoding="utf-8")
            close = raw.rfind(")")
            if close < 0:
                return None
            fields = raw[close + 1 :].split()
            if len(fields) < 20:
                return None
            starttime = fields[19]
            boot_id = ""
            try:
                boot_id = (
                    Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
                )
            except OSError:
                pass
            return f"linux:{boot_id}:{starttime}"
        except OSError:
            return None
    if not SUPPORTS_PROCESS_GROUPS:
        return None
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    token = (completed.stdout or "").strip()
    if not token or completed.returncode != 0:
        return None
    return f"ps:{token}"


def write_subagent_pid(scratch: Path, pid: int) -> None:
    """Record ``pid`` and its identity under ``scratch/subagent.pid``."""
    identity = process_identity(pid)
    if identity is None:
        return
    try:
        (scratch / "subagent.pid").write_text(f"{pid}\n{identity}\n", encoding="utf-8")
    except OSError:
        return


def read_subagent_pid(scratch: Path) -> tuple[int, str] | None:
    pid_path = scratch / "subagent.pid"
    try:
        lines = [
            line.strip()
            for line in pid_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError:
        return None
    if len(lines) < 2:
        return None
    try:
        pid = int(lines[0])
    except ValueError:
        return None
    identity = lines[1]
    if pid <= 0 or not identity:
        return None
    return pid, identity


def kill_recorded_subagent(scratch: Path) -> None:
    """Kill the process group recorded in ``scratch/subagent.pid``, if it is still that process."""
    recorded = read_subagent_pid(scratch)
    if recorded is None:
        return
    pid, expected_identity = recorded
    current = process_identity(pid)
    if current is not None and current != expected_identity:
        return
    if SUPPORTS_PROCESS_GROUPS:
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        return
