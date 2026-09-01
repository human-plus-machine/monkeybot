"""Cold-start gates (1c): import wall time and /health via ASGI (Story 8)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[1]

# OTEL env from observability tests can make a child process block on OTLP export.
_OTEL_ENV_PREFIXES = ("OTEL_", "MONKEYBOT_OTEL")


def _subprocess_env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_OTEL_ENV_PREFIXES)}
    env["PYTHONNOUSERSITE"] = "1"
    env.update(overrides)
    return env


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
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_subprocess_env(),
    )
    elapsed = time.perf_counter() - start
    assert elapsed < 1.5, f"import monkeybot.gateway.main took {elapsed:.3f}s (budget 1.5s CI)"


@pytest.mark.asyncio
async def test_health_under_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """First /health through ASGI stack should stay within the 1c budget."""
    from monkeybot.core.mcp.mcp_client import MCPClient
    from monkeybot.gateway.sse.app import app
    from monkeybot.observability import shutdown_observability

    shutdown_observability()

    async def _skip_mcp_load(self: MCPClient, _path: object, *_a: object, **_kw: object) -> None:
        return

    class _FastMemory:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def ensure_ready(self) -> None:
            return

        def register_hooks(self, _mgr: object) -> None:
            return

    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "model:\n  provider: fake\n  name: fake\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(MCPClient, "load_from_config", _skip_mcp_load)
    monkeypatch.setattr("monkeybot.gateway.sse.app.MemorySubsystem", _FastMemory)
    monkeypatch.setenv("DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "false")
    monkeypatch.setenv("MCP_CONFIG", "/nonexistent/mcp.json")
    monkeypatch.setenv("COMMAND_ALLOWLIST_CONFIG", "/nonexistent/command_allowlist.yaml")

    start = time.perf_counter()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get("/health")
    elapsed = time.perf_counter() - start
    assert resp.status_code == 200
    assert elapsed < 1.5, f"/health took {elapsed:.3f}s (budget 1.5s)"
