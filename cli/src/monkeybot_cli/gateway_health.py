"""Gateway health polling helpers shared by chat and talk."""

from __future__ import annotations

import socket
import subprocess
import time

import httpx


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Return True when ``host:port`` can be bound (nothing listening)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def wait_for_health(
    base: str, proc: subprocess.Popen[str] | None, timeout_s: float = 30.0
) -> bool:
    """Poll ``GET {base}/health`` until 200 from *our* child, process dies, or timeout.

    On success we re-check ``proc.poll()`` so a pre-existing listener on the same
    port cannot satisfy health while our spawn has already exited (failed bind).
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            resp = httpx.get(f"{base}/health", timeout=2.0)
            if resp.status_code == 200:
                if proc is not None and proc.poll() is not None:
                    return False
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False


def health_ok(base: str) -> bool:
    """Single-shot health check."""
    try:
        return httpx.get(f"{base}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


def occupied_gateway_message(base: str, port: int) -> str | None:
    """If ``port`` is already taken, return a user-facing error; else ``None``.

    Used before ``monkeybot chat`` spawns a gateway so we never talk to a stale
    process while the status bar shows the current yaml provider/model.
    """
    listening = not port_free(port)
    healthy = health_ok(base)
    if not listening and not healthy:
        return None
    detail = " (gateway /health OK)" if healthy else ""
    return (
        f"Port {port} is already in use{detail}. "
        f"Another monkeybot process is likely still running at {base}.\n"
        "Exit that session with /bye, stop `monkeybot run`, or free the port, "
        "then retry — or connect with `monkeybot chat --attach`."
    )
