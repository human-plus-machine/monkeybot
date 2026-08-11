"""Run a local shell command from the ``!`` prefix in ``monkeybot chat``.

Output is local-only — never sent to the agent.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"


def _popen_kwargs_for_platform() -> dict[str, object]:
    if _IS_WINDOWS:
        # getattr fallback: this constant only exists on Windows builds of
        # `subprocess`, so a plain attribute access breaks importing/testing
        # this module on POSIX even though the branch never runs there.
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _kill_process_tree(pid: int) -> None:
    if _IS_WINDOWS:
        # os.killpg doesn't exist on Windows; taskkill /T walks the tree
        # rooted at pid (which CREATE_NEW_PROCESS_GROUP made a group leader).
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_local_shell(command: str, cwd: Path, *, timeout: float = 120.0) -> tuple[str, int | None]:
    """Run ``command`` in a shell, returning merged stdout+stderr and the exit code.

    Runs as the root of its own process group/tree so a timeout can kill
    everything it spawned — plain ``subprocess.run(..., timeout=...)`` only
    kills the shell itself, leaving children it spawned (e.g. a backgrounded
    ``sleep``) running past the reported timeout.
    """
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **_popen_kwargs_for_platform(),
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)
        output, _ = proc.communicate()
        # ``None`` means timed out (distinct from a real exit status).
        return output + f"\n(timed out after {timeout:.0f}s)", None
    return output, proc.returncode


def truncate_output(text: str, *, max_lines: int = 200, max_chars: int = 10_000) -> str:
    """Bound output for transcript display, noting how much was dropped."""
    lines = text.splitlines()
    if len(lines) > max_lines:
        dropped = len(lines) - max_lines
        text = "\n".join(lines[:max_lines]) + f"\n… (+{dropped} lines truncated)"
    if len(text) > max_chars:
        dropped_chars = len(text) - max_chars
        text = text[:max_chars] + f"\n… (+{dropped_chars} chars truncated)"
    return text
