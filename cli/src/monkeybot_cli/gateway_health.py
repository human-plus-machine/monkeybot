"""Gateway health polling helpers shared by chat and talk."""

from __future__ import annotations

import subprocess
import time

import httpx


def wait_for_health(
    base: str, proc: subprocess.Popen[str] | None, timeout_s: float = 30.0
) -> bool:
    """Poll ``GET {base}/health`` until 200, process dies, or timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            resp = httpx.get(f"{base}/health", timeout=2.0)
            if resp.status_code == 200:
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
