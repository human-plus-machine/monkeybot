"""Shared helpers for spawning and tearing down process groups/trees."""

from __future__ import annotations

import os
import signal
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"


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
