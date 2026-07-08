"""Smoke tests for the realtime app factory."""

from __future__ import annotations

from monkeybot.gateway.realtime.app import create_realtime_app


def test_create_realtime_app_exposes_sse_routes() -> None:
    app = create_realtime_app()
    paths = app.openapi().get("paths", {})
    assert "/sessions" in paths, "SSE create-session route should be present"

    route_paths = {r.path for r in app.routes}
    assert "/sessions/{session_id}/realtime" in route_paths, "Realtime WebSocket route should be present"


def test_create_realtime_app_uses_combined_lifespan() -> None:
    app = create_realtime_app()
    assert app.router.lifespan_context is not None
