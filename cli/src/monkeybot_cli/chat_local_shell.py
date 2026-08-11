"""Run a local shell command from the ``!`` prefix in ``monkeybot chat``.

Output is local-only — never sent to the agent.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
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
    """Best-effort kill of the shell process group/tree.

    Swallow lookup/permission races — leader exit can make killpg raise
    ``PermissionError`` even though descendants still need a signal.
    """
    if _IS_WINDOWS:
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


def run_local_shell(command: str, cwd: Path, *, timeout: float = 120.0) -> tuple[str, int | None]:
    """Run ``command`` in a shell, returning merged stdout+stderr and the exit code.

    Runs as the root of its own process group/tree so a timeout can kill
    everything it spawned — plain ``subprocess.run(..., timeout=...)`` only
    kills the shell itself, leaving children it spawned (e.g. a backgrounded
    ``sleep``) running past the reported timeout.

    Output is capped at ``_MAX_CAPTURE_CHARS`` while streaming (never fully
    buffered first), so unbounded writers cannot exhaust memory before the
    TUI truncates for display. Works the same on POSIX and Windows.
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
    assert proc.stdout is not None

    chunks: list[str] = []
    state = {"total": 0, "capped": False}
    stop_reader = threading.Event()

    def _reader() -> None:
        stdout = proc.stdout
        if stdout is None:
            return
        try:
            while not stop_reader.is_set():
                chunk = stdout.read(_READ_CHUNK)
                if chunk == "":
                    break
                state["total"], hit = _append_capped(chunks, state["total"], chunk)
                if hit:
                    state["capped"] = True
                    _kill_process_tree(proc.pid)
                    break
        except (ValueError, OSError):
            # stdout closed from the main thread to unblock a stuck read.
            return

    reader = threading.Thread(target=_reader, name="monkeybot-local-shell-reader", daemon=True)
    reader.start()

    timed_out = False
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(proc.pid)

        # Shell may have exited while a descendant still holds the pipe —
        # kill the tree and close stdout so the reader cannot block forever.
        if reader.is_alive():
            _kill_process_tree(proc.pid)
            stop_reader.set()
            try:
                proc.stdout.close()
            except OSError:
                pass
            reader.join(timeout=2.0)
            if reader.is_alive() and not timed_out:
                # Still stuck after kill+close: treat as timeout so the TUI
                # always gets a bounded result instead of hanging.
                timed_out = True
        else:
            reader.join(timeout=0.1)
    except Exception:
        timed_out = True
        _kill_process_tree(proc.pid)
        stop_reader.set()
        try:
            proc.stdout.close()
        except OSError:
            pass
        reader.join(timeout=1.0)

    try:
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
            try:
                proc.wait(timeout=1)
            except Exception:
                pass
    except Exception:
        pass

    output = "".join(chunks)
    if timed_out:
        # ``None`` means timed out (distinct from a real exit status).
        return output + f"\n(timed out after {timeout:.0f}s)", None
    if state["capped"]:
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
