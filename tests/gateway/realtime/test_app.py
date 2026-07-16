"""Smoke tests for the realtime app factory."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from monkeybot.core.llm.provider import Message
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.types.content_blocks import Text
from monkeybot.gateway.realtime.app import create_realtime_app
from monkeybot.gateway.sse.app import GatewayLoopPort
from monkeybot.gateway.sse.session_bus import SessionRegistry


def test_create_realtime_app_exposes_sse_routes() -> None:
    app = create_realtime_app()
    paths = app.openapi().get("paths", {})
    assert "/sessions" in paths, "SSE create-session route should be present"

    route_paths = {r.path for r in app.routes}
    assert "/sessions/{session_id}/realtime" in route_paths, "Realtime WebSocket route should be present"
    assert "/realtime/health" in route_paths, "Realtime health snapshot should be present"
    assert "/realtime/sessions/{session_id}" in route_paths, "Realtime session lookup should be present"


def test_create_realtime_app_exposes_chat_history_delete() -> None:
    """The combined gateway used by the Mac app must support chat deletion."""
    app = create_realtime_app()
    methods: set[str] = set()
    for route in app.routes:
        if route.path == "/api/chat-history/{session_id}":
            methods.update(route.methods or set())

    assert "DELETE" in methods


@pytest.mark.asyncio
async def test_create_realtime_app_deletes_main_and_project_history() -> None:
    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    try:
        for session_id in ("main-agent-session", "project-agent-session"):
            await backend.history().append(
                session_id,
                Message(role="user", content=[Text(text=session_id)]),
            )

        app = create_realtime_app(registry=SessionRegistry(), loop_port=object())
        app.state.storage = backend
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.delete("/api/chat-history/main-agent-session")
            assert response.status_code == 200
            assert await backend.history().load("main-agent-session") == []
            assert len(await backend.history().load("project-agent-session")) == 1

            response = await client.delete("/api/chat-history/project-agent-session")
            assert response.status_code == 200
            assert await backend.history().load("project-agent-session") == []
    finally:
        await backend.close()


def test_create_realtime_app_uses_combined_lifespan() -> None:
    app = create_realtime_app()
    assert app.router.lifespan_context is not None


def test_create_realtime_app_wires_gateway_loop_port() -> None:
    """Chat /reply must use GatewayLoopPort, not the no-op default loop."""
    app = create_realtime_app()
    assert isinstance(app.state.loop, GatewayLoopPort)
    assert app.state.loop._serving_app() is app
