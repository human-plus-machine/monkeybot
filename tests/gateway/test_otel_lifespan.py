"""Gateway lifespan observability wiring."""

from __future__ import annotations

import pytest
from asgi_lifespan import LifespanManager

from monkeybot.gateway.sse.app import app


@pytest.mark.asyncio
async def test_gateway_lifespan_calls_observability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from monkeybot.core.mcp.mcp_client import MCPClient

    async def _skip_mcp_load(self: MCPClient, _path: object) -> None:
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

    async def _skip_mcp_load(self: MCPClient, _path: object) -> None:
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
