"""Gateway tests for scheduled-loop REST API."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.gateway.sse.routes import create_app
from monkeybot.gateway.sse.session_bus import SessionRegistry


@pytest.fixture
def scheduler_app():
    reg = SessionRegistry()

    class _NoopLoop:
        async def start_turn(self, session_id: str, request_id: str, user_content) -> None:
            del session_id, request_id, user_content

    app = create_app(registry=reg, loop_port=_NoopLoop())

    async def _open_storage() -> None:
        backend = SQLiteStorageBackend("sqlite:///:memory:")
        await backend.open()
        app.state.storage = backend

    return app, _open_storage


@pytest.mark.asyncio
async def test_scheduler_create_and_list_loop(scheduler_app) -> None:
    app, open_storage = scheduler_app
    await open_storage()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            "/scheduler/loops",
            json={
                "prompt": "BUSINESS: append status",
                "interval": "5s",
                "session_id": "loop-main",
                "loop_id": "demo-loop",
                "max_ticks": 3,
                "confirmed": True,
            },
        )
        assert create.status_code == 201
        body = create.json()
        assert body["loop"]["loop_id"] == "demo-loop"
        listed = await client.get("/scheduler/loops")
        assert listed.status_code == 200
        loops = listed.json()["loops"]
        assert any(row["loop_id"] == "demo-loop" for row in loops)


@pytest.mark.asyncio
async def test_scheduler_rejects_loop_without_guards(scheduler_app) -> None:
    app, open_storage = scheduler_app
    await open_storage()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/scheduler/loops",
            json={
                "prompt": "BUSINESS: append status",
                "interval": "5s",
                "session_id": "loop-main",
                "loop_id": "unguarded",
                "confirmed": True,
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_GUARDS"


@pytest.mark.asyncio
async def test_scheduler_requires_confirmation(scheduler_app) -> None:
    app, open_storage = scheduler_app
    await open_storage()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/scheduler/loops",
            json={
                "prompt": "BUSINESS: append status",
                "interval": "5s",
                "session_id": "loop-main",
                "loop_id": "unconfirmed",
                "max_ticks": 3,
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "CONFIRMATION_REQUIRED"


@pytest.mark.asyncio
async def test_scheduler_rejects_reserved_session_id(scheduler_app) -> None:
    """Regression for PR #179 review: scheduler-created sessions bypassed the
    POST /sessions reserved-prefix check entirely, so a loop's session_id
    could collide with the internal 'subagent:' namespace and silently
    disappear from list_threads/--continue with no indication why.
    """
    app, open_storage = scheduler_app
    await open_storage()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/scheduler/loops",
            json={
                "prompt": "BUSINESS: append status",
                "interval": "5s",
                "session_id": "subagent:hijacked",
                "loop_id": "reserved-loop",
                "max_ticks": 3,
                "confirmed": True,
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "BAD_REQUEST"
        assert "reserved prefix" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_scheduler_invoke_tick_rejects_reserved_session_id(scheduler_app) -> None:
    app, open_storage = scheduler_app
    await open_storage()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/scheduler/invoke-tick",
            json={
                "session_id": "subagent:direct-invoke",
                "request_id": "req-1",
                "message": "hi",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "BAD_REQUEST"
        assert "reserved prefix" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_scheduler_get_loop_includes_usage(scheduler_app) -> None:
    app, open_storage = scheduler_app
    await open_storage()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            "/scheduler/loops",
            json={
                "prompt": "BUSINESS: append status",
                "interval": "5s",
                "session_id": "loop-main",
                "loop_id": "usage-loop",
                "max_ticks": 3,
                "confirmed": True,
            },
        )
        assert create.status_code == 201
        detail = await client.get("/scheduler/loops/usage-loop")
        assert detail.status_code == 200
        body = detail.json()
        assert "usage" in body
        assert body["usage"]["session_id"] == "loop-main"
        assert body["usage"]["turns"] == 0
