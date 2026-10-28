"""Smoke test for the realtime gateway entrypoint module."""

from __future__ import annotations

from monkeybot.gateway.realtime_main import app


def test_realtime_main_app_imports() -> None:
    assert app is not None
    assert app.title == "monkeybot v2 Gateway"


def test_realtime_app_has_realtime_route() -> None:
    route_paths = {r.path for r in app.routes}
    assert "/sessions/{session_id}/realtime" in route_paths
