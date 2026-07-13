"""Gateway lifespan observability wiring."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager

from monkeybot.core.config import runtime_env
from monkeybot.gateway.sse.app import app


@pytest.mark.asyncio
async def test_gateway_lifespan_calls_observability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from monkeybot.core.mcp.mcp_client import MCPClient

    async def _skip_mcp_load(self: MCPClient, _path: object, *_a: object, **_kw: object) -> None:
        return

    monkeypatch.setattr(MCPClient, "load_from_config", _skip_mcp_load)
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "false")
    monkeypatch.setenv("DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("MCP_CONFIG", "/nonexistent/mcp.json")
    async with LifespanManager(app):
        pass


@pytest.mark.asyncio
async def test_gateway_startup_with_bad_otlp(monkeypatch: pytest.MonkeyPatch) -> None:
    from monkeybot.core.mcp.mcp_client import MCPClient

    async def _skip_mcp_load(self: MCPClient, _path: object, *_a: object, **_kw: object) -> None:
        return

    def _raise_processor(_kind: str) -> object:
        raise RuntimeError("bad endpoint")

    monkeypatch.setattr(MCPClient, "load_from_config", _skip_mcp_load)
    monkeypatch.setattr("monkeybot.observability._create_span_processor", _raise_processor)
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp")
    monkeypatch.setenv("DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("MCP_CONFIG", "/nonexistent/mcp.json")

    from monkeybot.observability import is_observability_enabled, shutdown_observability

    shutdown_observability()
    async with LifespanManager(app):
        assert is_observability_enabled() is False


@pytest.mark.asyncio
async def test_asgi_lifespan_loads_agent_dotenv_before_observability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ASGI imports defer bootstrap, but startup must load root .env first."""
    from monkeybot.core.mcp.mcp_client import MCPClient
    agent = tmp_path / "agent"
    config_dir = agent / "monkeybot_config"
    config_dir.mkdir(parents=True)
    (config_dir / "monkeybot.yaml").write_text(
        "model:\n  provider: fake\n"
        "web_search:\n  backend: none\n"
        "memory_hook:\n  enabled: false\n"
        "paths:\n  db_url: 'sqlite:///:memory:'\n",
        encoding="utf-8",
    )
    (agent / ".env").write_text("MONKEYBOT_OTEL_ENABLED=true\n", encoding="utf-8")

    async def _skip_mcp_load(self: MCPClient, _path: object, *_a: object, **_kw: object) -> None:
        return

    observed: list[str | None] = []

    def _observe_environment() -> bool:
        observed.append(os.environ.get("MONKEYBOT_OTEL_ENABLED"))
        return False

    before = dict(os.environ)
    try:
        runtime_env.reset_runtime_env_state_for_tests()
        monkeypatch.chdir(agent)
        monkeypatch.delenv("MONKEYBOT_CONFIG", raising=False)
        monkeypatch.delenv("MONKEYBOT_OTEL_ENABLED", raising=False)
        monkeypatch.setattr(MCPClient, "load_from_config", _skip_mcp_load)
        monkeypatch.setattr("monkeybot.observability.init_observability", _observe_environment)
        async with LifespanManager(app):
            pass
        assert observed == ["true"]
    finally:
        os.environ.clear()
        os.environ.update(before)
        runtime_env.reset_runtime_env_state_for_tests()
