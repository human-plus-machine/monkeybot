"""Cold-start gates (1c): import wall time and /health via ASGI (Story 8)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_import_gateway_main_under_budget() -> None:
    """Integrated entry `monkeybot.gateway.main` must import quickly (1c deploy path)."""
    start = time.perf_counter()
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import monkeybot.gateway.main",
        ],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
        env={**os.environ, "PYTHONNOUSERSITE": "1"},
    )
    elapsed = time.perf_counter() - start
    assert elapsed < 1.5, f"import monkeybot.gateway.main took {elapsed:.3f}s (budget 1.5s CI)"


def test_health_under_budget_via_subprocess() -> None:
    """First /health through ASGI stack should stay within the 1c budget."""
    script = r"""
import asyncio
import os
import time

os.environ.setdefault("DB_URL", "sqlite:///:memory:")
os.environ.setdefault("MODEL_PROVIDER", "fake")
os.environ.setdefault("MCP_CONFIG", "/nonexistent/mcp.json")
os.environ.setdefault("COMMAND_ALLOWLIST_CONFIG", "/nonexistent/command_allowlist.yaml")

async def run():
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient
    from monkeybot.gateway.sse.app import app

    async with LifespanManager(app):
        t0 = time.perf_counter()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/health")
        elapsed = time.perf_counter() - t0
        assert r.status_code == 200
        assert elapsed < 1.5, elapsed

asyncio.run(run())
"""
    start = time.perf_counter()
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
    )
    wall = time.perf_counter() - start
    assert wall < 3.0, f"subprocess health probe took {wall:.3f}s"
