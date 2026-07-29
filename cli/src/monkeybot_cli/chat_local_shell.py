"""Run a local shell command from the ``!`` prefix in ``monkeybot chat``.

Output is local-only — never sent to the agent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_local_shell(command: str, cwd: Path, *, timeout: float = 120.0) -> tuple[str, int | None]:
    """Run ``command`` in a shell, returning merged stdout+stderr and the exit code."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return stdout + stderr + f"\n(timed out after {timeout:.0f}s)", None
    output = result.stdout + result.stderr
    return output, result.returncode


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
