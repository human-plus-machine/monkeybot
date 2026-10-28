"""Gateway shutdown closes process-wide SDK HTTP clients."""

from __future__ import annotations

from typing import Any

import pytest
from asgi_lifespan import LifespanManager

from monkeybot.gateway.sse.app import app
from monkeybot.providers._sdk_client import cached_sdk_client


@pytest.mark.asyncio
async def test_gateway_shutdown_closes_cached_sdk_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from monkeybot.core.mcp.mcp_client import MCPClient

    async def _skip_mcp_load(self: MCPClient, _path: object, *_a: object, **_kw: object) -> None:
        return

    class _OpenAIShape:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(MCPClient, "load_from_config", _skip_mcp_load)
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "false")
    monkeypatch.setenv("DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("MCP_CONFIG", "/nonexistent/mcp.json")

    holder: dict[str, Any] = {}
    async with LifespanManager(app):
        holder["client"] = cached_sdk_client(("gateway-shutdown",), _OpenAIShape)

    assert holder["client"].closed is True


@pytest.mark.asyncio
async def test_gateway_shutdown_closes_sdk_clients_when_storage_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CPython's sqlite3 raises this exact text when a closed connection is used."""
    import sqlite3

    from monkeybot.core.mcp.mcp_client import MCPClient

    async def _skip_mcp_load(self: MCPClient, _path: object, *_a: object, **_kw: object) -> None:
        return

    class _OpenAIShape:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class _ClosedStorage:
        async def close(self) -> None:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")

    monkeypatch.setattr(MCPClient, "load_from_config", _skip_mcp_load)
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "false")
    monkeypatch.setenv("DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("MCP_CONFIG", "/nonexistent/mcp.json")

    holder: dict[str, Any] = {}
    with pytest.raises(sqlite3.ProgrammingError):
        async with LifespanManager(app):
            holder["client"] = cached_sdk_client(("gateway-storage-fails",), _OpenAIShape)
            real_storage = app.state.storage
            monkeypatch.setattr(app.state, "storage", _ClosedStorage())

    await real_storage.close()
    assert holder["client"].closed is True
