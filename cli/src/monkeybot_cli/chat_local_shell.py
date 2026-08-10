"""Run a local shell command from the ``!`` prefix in ``monkeybot chat``.

Output is local-only — never sent to the agent.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path


def run_local_shell(command: str, cwd: Path, *, timeout: float = 120.0) -> tuple[str, int | None]:
    """Run ``command`` in a shell, returning merged stdout+stderr and the exit code.

    Runs in its own process group (``start_new_session=True``) so a timeout
    kills the whole group via ``os.killpg`` — plain ``subprocess.run(...,
    timeout=...)`` only kills the shell itself, leaving any children it
    spawned (e.g. a backgrounded ``sleep``) running past the reported timeout.
    """
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = proc.communicate()
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
