"""The realtime endpoint claims its session against concurrent history rewrites."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from monkeybot.core.config.realtime_config import RealtimeConfig
from monkeybot.gateway.realtime.deps import RealtimeDependencies
from monkeybot.gateway.realtime.manager import RealtimeSessionManager
from monkeybot.gateway.realtime.routes import create_realtime_router


class _FailingStorage:
    """Fails the connect right after the session is claimed."""

    def __init__(self) -> None:
        self.reads = 0

    def history(self) -> Any:
        self.reads += 1
        raise RuntimeError("storage down")


def _client(manager: RealtimeSessionManager, storage: _FailingStorage) -> TestClient:
    deps = RealtimeDependencies()
    deps.realtime_provider = object()  # type: ignore[assignment]
    deps.storage = storage  # type: ignore[assignment]
    app = FastAPI()
    app.include_router(create_realtime_router(deps, manager))
    return TestClient(app)


def _connect(client: TestClient, session_id: str) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(f"/sessions/{session_id}/realtime") as ws,
    ):
        ws.receive_json()
        ws.receive_json()


def test_failed_connect_releases_its_claim() -> None:
    manager = RealtimeSessionManager(RealtimeConfig())
    storage = _FailingStorage()
    _connect(_client(manager, storage), "s1")
    assert storage.reads == 1
    assert manager.begin_rewrite("s1")


def test_connect_is_refused_during_a_rewrite() -> None:
    manager = RealtimeSessionManager(RealtimeConfig())
    storage = _FailingStorage()
    assert manager.begin_rewrite("s1")
    _connect(_client(manager, storage), "s1")
    assert storage.reads == 0
    assert manager.claim("s1") is False
    manager.end_rewrite("s1")
    assert manager.claim("s1")
