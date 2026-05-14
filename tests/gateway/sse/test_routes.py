"""Tests for v2 SSE gateway routes.

JSON routes use ``httpx.AsyncClient`` + ``ASGITransport`` (fine for finite bodies).

**Long-lived SSE:** In-process clients buffer until the ASGI response completes, so
``GET /sessions/{id}/events`` cannot be exercised as an infinite stream here. Replay,
pings, and the live subscriber queue are covered in ``test_session_bus.py``; agent
frames over the bus are covered in ``tests/integration/test_mb_e2e_simple_reply.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from monkeybot.gateway.sse.routes import create_app
from monkeybot.gateway.sse.session_bus import SessionRegistry
from monkeybot.gateway.sse.sse import agent_event_to_wire_dict, json_dumps_wire


class FakeLoopPort:
    """Publishes a short canned event sequence then clears busy state."""

    def __init__(self, registry: SessionRegistry) -> None:
        self._registry = registry

    async def start_turn(
        self,
        session_id: str,
        request_id: str,
        message: str,
    ) -> None:
        _ = message
        bus = self._registry.get(session_id)
        if bus is None:
            return
        await bus.publish_data(
            json_dumps_wire(agent_event_to_wire_dict({"kind": "Thinking", "request_id": request_id}))
        )
        await bus.publish_data(
            json_dumps_wire(
                agent_event_to_wire_dict(
                    {"kind": "AssistantDelta", "request_id": request_id, "delta": "hi"},
                )
            )
        )
        await bus.publish_data(
            json_dumps_wire(
                agent_event_to_wire_dict(
                    {
                        "kind": "TurnComplete",
                        "request_id": request_id,
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 2,
                            "cached_tokens": 0,
                            "cost_usd": 0.0,
                            "duration_ms": 1,
                        },
                    },
                )
            )
        )
        bus.current_request_id = None


class HoldingLoopPort:
    """Never completes, so the session stays busy until cancel/side effect."""

    def __init__(self) -> None:
        self._hold = asyncio.Event()

    async def start_turn(
        self,
        session_id: str,
        request_id: str,
        message: str,
    ) -> None:
        _ = (session_id, request_id, message)
        await self._hold.wait()


@pytest.fixture
def registry() -> SessionRegistry:
    return SessionRegistry()


@pytest.fixture
def app(registry: SessionRegistry):
    return create_app(loop_port=FakeLoopPort(registry), registry=registry)


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_post_session_returns_201(
    client: AsyncClient,
) -> None:
    r = await client.post("/sessions", json={})
    assert r.status_code == 201
    body = r.json()
    assert "session_id" in body
    assert isinstance(body["created_at"], int)


@pytest.mark.asyncio
async def test_duplicate_session_returns_409(
    client: AsyncClient,
) -> None:
    r1 = await client.post("/sessions", json={"session_id": "fixed-id"})
    assert r1.status_code == 201
    r2 = await client.post("/sessions", json={"session_id": "fixed-id"})
    assert r2.status_code == 409
    err = r2.json()["error"]
    assert err["code"] == "SESSION_ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_reply_returns_409_when_busy(
    registry: SessionRegistry,
) -> None:
    app = create_app(loop_port=HoldingLoopPort(), registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cr = await client.post("/sessions", json={})
        sid = cr.json()["session_id"]
        r1 = await client.post(
            f"/sessions/{sid}/reply",
            json={"request_id": "a", "message": "one"},
        )
        assert r1.status_code == 200
        r2 = await client.post(
            f"/sessions/{sid}/reply",
            json={"request_id": "b", "message": "two"},
        )
        assert r2.status_code == 409
        assert r2.json()["error"]["code"] == "SESSION_BUSY"


@pytest.mark.asyncio
async def test_oversized_reply_message_returns_400(
    client: AsyncClient,
) -> None:
    cr = await client.post("/sessions", json={})
    sid = cr.json()["session_id"]
    huge = "x" * 32001
    r = await client.post(
        f"/sessions/{sid}/reply",
        json={"request_id": "rid", "message": huge},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_health_returns_200(app) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["version"] == "2.0.0"


def test_get_events_returns_404_for_unknown_session(registry: SessionRegistry) -> None:
    app = create_app(loop_port=FakeLoopPort(registry), registry=registry)
    with TestClient(app) as client:
        r = client.get("/sessions/does-not-exist/events")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_get_usage_returns_json_for_existing_session(
    client: AsyncClient,
) -> None:
    cr = await client.post("/sessions", json={})
    sid = cr.json()["session_id"]
    r = await client.get(f"/sessions/{sid}/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == sid
    assert body["turns"] == 0
    assert body["last_prompt_tokens"] == 0
    assert isinstance(body["context_window_tokens"], int)
    assert body["context_window_tokens"] >= 1


@pytest.mark.asyncio
async def test_get_usage_404_for_unknown_session(client: AsyncClient) -> None:
    r = await client.get("/sessions/missing-session/usage")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SESSION_NOT_FOUND"
