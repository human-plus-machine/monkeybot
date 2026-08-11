"""Run a local shell command from the ``!`` prefix in ``monkeybot chat``.

Output is local-only — never sent to the agent.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"

# Cap capture before TUI truncation so `!yes`-style floods cannot OOM the process.
_MAX_CAPTURE_CHARS = 100_000
_READ_CHUNK = 4_096


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


def _drain_and_reap(proc: subprocess.Popen[str]) -> None:
    """Best-effort drain + wait so pipe-backed children do not hang."""
    if proc.stdout is not None:
        try:
            while proc.stdout.read(_READ_CHUNK):
                pass
        except Exception:
            pass
    try:
        proc.wait(timeout=2)
    except Exception:
        _kill_process_tree(proc.pid)
        try:
            proc.wait(timeout=1)
        except Exception:
            pass


def _append_capped(chunks: list[str], total: int, data: str) -> tuple[int, bool]:
    """Append ``data`` up to the capture cap. Returns (new_total, hit_cap)."""
    remain = _MAX_CAPTURE_CHARS - total
    if remain <= 0:
        return total, True
    if len(data) <= remain:
        chunks.append(data)
        return total + len(data), False
    chunks.append(data[:remain])
    return total + remain, True


def _read_output_posix(proc: subprocess.Popen[str], *, deadline: float) -> tuple[str, bool, bool]:
    """Return ``(output, timed_out, capped)`` using select-bounded reads."""
    assert proc.stdout is not None
    chunks: list[str] = []
    total = 0
    timed_out = False
    capped = False
    while True:
        now = time.monotonic()
        if now >= deadline:
            timed_out = True
            break
        ready, _, _ = select.select([proc.stdout], [], [], min(0.2, deadline - now))
        if not ready:
            if proc.poll() is not None:
                rest = proc.stdout.read()
                if rest:
                    total, capped = _append_capped(chunks, total, rest)
                break
            continue
        chunk = proc.stdout.read(_READ_CHUNK)
        if chunk == "":
            break
        total, capped = _append_capped(chunks, total, chunk)
        if capped:
            break
    return "".join(chunks), timed_out, capped


def _read_output_windows(proc: subprocess.Popen[str], *, timeout: float) -> tuple[str, bool, bool]:
    """Windows pipes are not select()-able; use communicate with a post-cap."""
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc.pid)
        output, _ = proc.communicate()
    capped = len(output) > _MAX_CAPTURE_CHARS
    if capped:
        output = output[:_MAX_CAPTURE_CHARS]
    return output, timed_out, capped


def run_local_shell(command: str, cwd: Path, *, timeout: float = 120.0) -> tuple[str, int | None]:
    """Run ``command`` in a shell, returning merged stdout+stderr and the exit code.

    Runs as the root of its own process group/tree so a timeout can kill
    everything it spawned — plain ``subprocess.run(..., timeout=...)`` only
    kills the shell itself, leaving children it spawned (e.g. a backgrounded
    ``sleep``) running past the reported timeout.

    Output is capped at ``_MAX_CAPTURE_CHARS`` so unbounded writers cannot
    exhaust memory before the TUI truncates for display.
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
    deadline = time.monotonic() + timeout
    try:
        if _IS_WINDOWS:
            output, timed_out, capped = _read_output_windows(proc, timeout=timeout)
        else:
            output, timed_out, capped = _read_output_posix(proc, deadline=deadline)
            if timed_out or capped:
                _kill_process_tree(proc.pid)
            _drain_and_reap(proc)
    except Exception:
        _kill_process_tree(proc.pid)
        _drain_and_reap(proc)
        raise

    if timed_out:
        # ``None`` means timed out (distinct from a real exit status).
        return output + f"\n(timed out after {timeout:.0f}s)", None
    if capped:
        output += f"\n… (output capped at {_MAX_CAPTURE_CHARS} chars)"
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
