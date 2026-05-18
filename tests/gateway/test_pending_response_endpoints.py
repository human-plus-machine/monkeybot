"""Tests for Story 5 pending UI response POST endpoints and bus wait helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from monkeybot.gateway.sse.routes import create_app
from monkeybot.gateway.sse.session_bus import (
    SessionRegistry,
    _await_user_response,
)


@pytest.fixture
def registry() -> SessionRegistry:
    return SessionRegistry()


@pytest.fixture
async def client(registry: SessionRegistry) -> AsyncIterator[AsyncClient]:
    app = create_app(registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_post_tool_confirmation_202_registers_resolution(
    client: AsyncClient,
    registry: SessionRegistry,
) -> None:
    cr = await client.post("/sessions", json={})
    sid = cr.json()["session_id"]
    bus = registry.get(sid)
    assert bus is not None
    fut = bus.register_pending("c1")
    r = await client.post(
        f"/sessions/{sid}/tool-confirmations/c1",
        json={"approved": True},
    )
    assert r.status_code == 202
    assert r.json() == {"ok": True}
    assert fut.done()
    assert fut.result() == {"approved": True}


@pytest.mark.asyncio
async def test_post_tool_confirmation_with_reason_denial(
    client: AsyncClient,
    registry: SessionRegistry,
) -> None:
    cr = await client.post("/sessions", json={})
    sid = cr.json()["session_id"]
    bus = registry.get(sid)
    assert bus is not None
    fut = bus.register_pending("c1")
    r = await client.post(
        f"/sessions/{sid}/tool-confirmations/c1",
        json={"approved": False, "reason": "looks unsafe"},
    )
    assert r.status_code == 202
    assert fut.result()["approved"] is False
    assert fut.result()["reason"] == "looks unsafe"


@pytest.mark.asyncio
async def test_post_tool_confirmation_404_unknown_session(client: AsyncClient) -> None:
    r = await client.post(
        "/sessions/not-a-real-session/tool-confirmations/c1",
        json={"approved": True},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_post_tool_confirmation_404_unknown_id(
    client: AsyncClient,
    registry: SessionRegistry,
) -> None:
    cr = await client.post("/sessions", json={})
    sid = cr.json()["session_id"]
    bus = registry.get(sid)
    assert bus is not None
    bus.register_pending("other-id")
    r = await client.post(
        f"/sessions/{sid}/tool-confirmations/unknown-tool-call",
        json={"approved": True},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_post_tool_confirmation_409_when_already_terminal(
    client: AsyncClient,
    registry: SessionRegistry,
) -> None:
    cr = await client.post("/sessions", json={})
    sid = cr.json()["session_id"]
    bus = registry.get(sid)
    assert bus is not None
    bus.register_pending("c1")
    r1 = await client.post(
        f"/sessions/{sid}/tool-confirmations/c1",
        json={"approved": True},
    )
    assert r1.status_code == 202
    r2 = await client.post(
        f"/sessions/{sid}/tool-confirmations/c1",
        json={"approved": True},
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_post_elicitation_202_with_user_data(
    client: AsyncClient,
    registry: SessionRegistry,
) -> None:
    cr = await client.post("/sessions", json={})
    sid = cr.json()["session_id"]
    bus = registry.get(sid)
    assert bus is not None
    fut = bus.register_pending("e1")
    r = await client.post(
        f"/sessions/{sid}/elicitations/e1",
        json={"user_data": {"name": "x"}},
    )
    assert r.status_code == 202
    assert fut.done()


@pytest.mark.asyncio
async def test_post_elicitation_202_with_null_user_data(
    client: AsyncClient,
    registry: SessionRegistry,
) -> None:
    cr = await client.post("/sessions", json={})
    sid = cr.json()["session_id"]
    bus = registry.get(sid)
    assert bus is not None
    fut = bus.register_pending("e1")
    r = await client.post(
        f"/sessions/{sid}/elicitations/e1",
        json={"user_data": None},
    )
    assert r.status_code == 202
    assert fut.done()


@pytest.mark.asyncio
async def test_post_frontend_tool_400_unknown_block_type(
    client: AsyncClient,
    registry: SessionRegistry,
) -> None:
    cr = await client.post("/sessions", json={})
    sid = cr.json()["session_id"]
    bus = registry.get(sid)
    assert bus is not None
    fut = bus.register_pending("ft1")
    r = await client.post(
        f"/sessions/{sid}/frontend-tool-results/ft1",
        json={"result": [{"type": "futureBlock"}], "is_error": False},
    )
    assert r.status_code == 400
    assert not fut.done()


@pytest.mark.asyncio
async def test_post_frontend_tool_202_after_recovery(
    client: AsyncClient,
    registry: SessionRegistry,
) -> None:
    cr = await client.post("/sessions", json={})
    sid = cr.json()["session_id"]
    bus = registry.get(sid)
    assert bus is not None
    fut = bus.register_pending("ft1")
    r_bad = await client.post(
        f"/sessions/{sid}/frontend-tool-results/ft1",
        json={"result": [{"type": "futureBlock"}], "is_error": False},
    )
    assert r_bad.status_code == 400
    r_ok = await client.post(
        f"/sessions/{sid}/frontend-tool-results/ft1",
        json={"result": [{"type": "text", "text": "ok"}], "is_error": False},
    )
    assert r_ok.status_code == 202
    assert fut.done()


@pytest.mark.asyncio
async def test_post_frontend_tool_202_text_block(
    client: AsyncClient,
    registry: SessionRegistry,
) -> None:
    cr = await client.post("/sessions", json={})
    sid = cr.json()["session_id"]
    bus = registry.get(sid)
    assert bus is not None
    fut = bus.register_pending("ft1")
    r = await client.post(
        f"/sessions/{sid}/frontend-tool-results/ft1",
        json={"result": [{"type": "text", "text": "echoed"}], "is_error": False},
    )
    assert r.status_code == 202
    assert fut.done()


@pytest.mark.asyncio
async def test_cancel_post_cancels_all_pending_futures(
    client: AsyncClient,
    registry: SessionRegistry,
) -> None:
    cr = await client.post("/sessions", json={})
    sid = cr.json()["session_id"]
    bus = registry.get(sid)
    assert bus is not None
    f1 = bus.register_pending("a")
    f2 = bus.register_pending("b")
    r = await client.post(
        f"/sessions/{sid}/cancel",
        json={"request_id": "rid"},
    )
    assert r.status_code == 200
    assert f1.cancelled() or f1.done()
    assert f2.cancelled() or f2.done()


@pytest.mark.asyncio
async def test_await_user_response_timeout_sentinel(registry: SessionRegistry) -> None:
    reg = registry
    created = reg.create("only", agent_md=None, created_at_ms=0)
    created.register_pending("pk")
    out = await _await_user_response(created, pending_key="pk", timeout_sec=0.05)
    assert out == {"_timeout": True}
    assert "pk" in created.terminated_pending_keys


@pytest.mark.asyncio
async def test_await_user_response_cancel_raises(registry: SessionRegistry) -> None:
    reg = registry
    bus = reg.create("s2", agent_md=None, created_at_ms=0)
    fut = bus.register_pending("pk")
    fut.cancel()
    with pytest.raises(asyncio.CancelledError):
        await _await_user_response(bus, pending_key="pk", timeout_sec=1.0)
