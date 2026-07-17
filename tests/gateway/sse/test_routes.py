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

from monkeybot.core.runtime.events import AssistantDelta, Thinking, TurnComplete, UsageTotals
from monkeybot.core.types.content_blocks import Text
from monkeybot.gateway.sse.loop_port import UsagePort
from monkeybot.gateway.sse.models import SessionUsageResponse
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
        user_content: list[Text],
    ) -> None:
        _ = user_content
        bus = self._registry.get(session_id)
        if bus is None:
            return
        await bus.publish_data(
            json_dumps_wire(agent_event_to_wire_dict(Thinking(request_id=request_id)))
        )
        await bus.publish_data(
            json_dumps_wire(
                agent_event_to_wire_dict(
                    AssistantDelta(request_id=request_id, delta="hi"),
                )
            )
        )
        await bus.publish_data(
            json_dumps_wire(
                agent_event_to_wire_dict(
                    TurnComplete(
                        request_id=request_id,
                        usage=UsageTotals(
                            input_tokens=1,
                            output_tokens=2,
                            cached_tokens=0,
                            cost_usd=0.0,
                            duration_ms=1,
                            estimated_prompt_tokens=0,
                        ),
                    ),
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
        user_content: list[Text],
    ) -> None:
        _ = (session_id, request_id, user_content)
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
async def test_delete_session_returns_200_and_removes_it(
    client: AsyncClient,
    registry: SessionRegistry,
) -> None:
    cr = await client.post("/sessions", json={})
    sid = cr.json()["session_id"]
    assert registry.get(sid) is not None

    r = await client.delete(f"/sessions/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is True
    assert body["transcript_report_dir"] is None
    assert registry.get(sid) is None


@pytest.mark.asyncio
async def test_delete_unknown_session_is_idempotent_200(
    client: AsyncClient,
) -> None:
    r = await client.delete("/sessions/does-not-exist")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is False
    assert body["transcript_report_dir"] is None


@pytest.mark.asyncio
async def test_delete_session_writes_transcript_report(
    client: AsyncClient,
    registry: SessionRegistry,
    tmp_path,
) -> None:
    from pathlib import Path

    from monkeybot.core.persistence.transcript import TranscriptWriter

    cr = await client.post("/sessions", json={})
    sid = cr.json()["session_id"]
    bus = registry.get(sid)
    assert bus is not None
    writer = TranscriptWriter(sid, workspace_root=Path(tmp_path))
    await writer.ensure_manifest(model="gpt-test", provider="fake")
    await writer.write_user_message(request_id="r1", content="hello")
    bus.transcript_writer = writer

    r = await client.delete(f"/sessions/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is True
    assert body["transcript_report_dir"] is not None
    report_dir = Path(body["transcript_report_dir"])
    assert (report_dir / "transcript.ndjson").is_file()
    assert (report_dir / "brief.md").is_file()
    assert (report_dir / "report.json").is_file()
    assert (report_dir / "meta.json").is_file()
    brief = (report_dir / "brief.md").read_text(encoding="utf-8")
    assert "## Session summary" in brief


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


def test_session_usage_response_defaults_cache_fields() -> None:
    raw = {
        "session_id": "s1",
        "turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "cost_usd": 0.0,
        "period_start": 0,
        "period_end": 0,
        "last_prompt_tokens": 0,
        "estimated_prompt_tokens": 0,
        "summarization_threshold_tokens": 170_000,
        "context_window_tokens": 200_000,
    }
    resp = SessionUsageResponse.model_validate(raw)
    assert resp.cache_read_tokens == 0
    assert resp.cache_creation_tokens == 0


def test_session_usage_response_includes_cache_fields() -> None:
    raw = {
        "session_id": "s1",
        "turns": 2,
        "input_tokens": 10,
        "output_tokens": 5,
        "cached_tokens": 14,
        "cache_read_tokens": 10,
        "cache_creation_tokens": 4,
        "cost_usd": 0.01,
        "period_start": 1,
        "period_end": 2,
        "last_prompt_tokens": 3,
        "estimated_prompt_tokens": 4,
        "summarization_threshold_tokens": 170_000,
        "context_window_tokens": 200_000,
    }
    resp = SessionUsageResponse.model_validate(raw)
    assert resp.cache_read_tokens == 10
    assert resp.cache_creation_tokens == 4


class _PopulatedUsagePort:
    async def session_usage(
        self,
        session_id: str,
        *,
        since: str | None,
    ) -> dict[str, object]:
        _ = since
        return {
            "session_id": session_id,
            "turns": 2,
            "input_tokens": 10,
            "output_tokens": 5,
            "cached_tokens": 28,
            "cache_read_tokens": 20,
            "cache_creation_tokens": 8,
            "cost_usd": 0.05,
            "period_start": 1000,
            "period_end": 2000,
            "last_prompt_tokens": 3,
            "estimated_prompt_tokens": 4,
            "summarization_threshold_tokens": 170_000,
            "context_window_tokens": 200_000,
        }


@pytest.mark.asyncio
async def test_get_usage_zero_payload_has_cache_keys(
    registry: SessionRegistry,
) -> None:
    app = create_app(loop_port=FakeLoopPort(registry), registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cr = await client.post("/sessions", json={})
        sid = cr.json()["session_id"]
        r = await client.get(f"/sessions/{sid}/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["cache_read_tokens"] == 0
    assert body["cache_creation_tokens"] == 0


@pytest.mark.asyncio
async def test_get_usage_populated_payload_has_cache_split(
    registry: SessionRegistry,
) -> None:
    usage_port: UsagePort = _PopulatedUsagePort()
    app = create_app(
        loop_port=FakeLoopPort(registry),
        usage_port=usage_port,
        registry=registry,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cr = await client.post("/sessions", json={})
        sid = cr.json()["session_id"]
        r = await client.get(f"/sessions/{sid}/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["cache_read_tokens"] == 20
    assert body["cache_creation_tokens"] == 8


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
    assert body["estimated_prompt_tokens"] == 0
    assert isinstance(body["summarization_threshold_tokens"], int)
    assert body["summarization_threshold_tokens"] >= 1
    assert isinstance(body["context_window_tokens"], int)
    assert body["context_window_tokens"] >= 1


@pytest.mark.asyncio
async def test_get_usage_404_for_unknown_session(client: AsyncClient) -> None:
    r = await client.get("/sessions/missing-session/usage")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_workspace_tree_and_file(
    registry: SessionRegistry,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("MONKEYBOT_AGENT_ROOT", str(tmp_path))
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_ROOT", str(workspace))
    (workspace / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    sub = workspace / "nested"
    sub.mkdir()
    (sub / "inner.txt").write_text("inside", encoding="utf-8")

    app = create_app(loop_port=FakeLoopPort(registry), registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/workspace/tree")
        assert r.status_code == 200
        root = r.json()
        assert root["path"] == "."
        names = {e["name"] for e in root["entries"]}
        assert "hello.txt" in names
        assert "nested" in names

        r2 = await client.get("/api/workspace/tree", params={"path": "nested"})
        assert r2.status_code == 200
        assert {e["name"] for e in r2.json()["entries"]} == {"inner.txt"}

        rf = await client.get("/api/workspace/file", params={"path": "hello.txt"})
        assert rf.status_code == 200
        body = rf.json()
        assert body["path"] == "hello.txt"
        assert body["total_lines"] == 2
        assert "alpha" in body["content"]

        r404 = await client.get("/api/workspace/tree", params={"path": "missing-dir"})
        assert r404.status_code == 404

        r_bad = await client.get("/api/workspace/tree", params={"path": ".."})
        assert r_bad.status_code == 400


@pytest.mark.asyncio
async def test_workspace_prefers_workspace_subdirectory(
    registry: SessionRegistry,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """When ``<cwd>/workspace`` exists, file API is rooted there (sibling harness dirs stay hidden)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "monkeybot_config").mkdir()
    (tmp_path / "monkeybot_config" / "secret.yaml").write_text("nope", encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("MONKEYBOT_AGENT_ROOT", str(tmp_path))
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_ROOT", str(ws))
    (ws / "hello.txt").write_text("in-workspace", encoding="utf-8")

    app = create_app(loop_port=FakeLoopPort(registry), registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/workspace/tree")
        assert r.status_code == 200
        names = {e["name"] for e in r.json()["entries"]}
        assert "hello.txt" in names
        assert "monkeybot_config" not in names


@pytest.mark.asyncio
async def test_workspace_disabled_returns_404(
    registry: SessionRegistry,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_API", "0")
    app = create_app(loop_port=FakeLoopPort(registry), registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/workspace/tree")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_workspace_file_requires_path(registry: SessionRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    app = create_app(loop_port=FakeLoopPort(registry), registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/workspace/file", params={"path": "  "})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_chat_history_list_and_detail(registry: SessionRegistry) -> None:
    from monkeybot.core.llm.provider import Message
    from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
    from monkeybot.core.types.content_blocks import Text

    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    try:
        hist = backend.history()
        for session_id in ("main-agent-session", "project-agent-session"):
            await hist.append(
                session_id,
                Message(role="user", content=[Text(text=f"hello {session_id}")]),
            )
            await hist.append(
                session_id,
                Message(role="assistant", content=[Text(text="hi there")]),
            )

        app = create_app(loop_port=FakeLoopPort(registry), registry=registry)
        app.state.storage = backend
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.get("/api/chat-history")
            assert r.status_code == 200
            threads = r.json()["threads"]
            assert {t["session_id"] for t in threads} == {
                "main-agent-session",
                "project-agent-session",
            }

            rd = await client.get("/api/chat-history/main-agent-session")
            assert rd.status_code == 200
            body = rd.json()
            assert body["session_id"] == "main-agent-session"
            assert body["messages"][0]["role"] == "user"
            assert "hello main-agent-session" in body["messages"][0]["text"]
            assert body["messages"][1]["role"] == "assistant"

            deleted = await client.delete("/api/chat-history/main-agent-session")
            assert deleted.status_code == 200
            assert deleted.json() == {"deleted": True}
            assert await hist.load("main-agent-session") == []
            assert len(await hist.load("project-agent-session")) == 2

            deleted = await client.delete("/api/chat-history/project-agent-session")
            assert deleted.status_code == 200
            assert deleted.json() == {"deleted": True}
            assert await hist.load("project-agent-session") == []

            remaining = await client.get("/api/chat-history")
            assert remaining.json()["threads"] == []
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_chat_history_detail_includes_thinking(registry: SessionRegistry) -> None:
    from monkeybot.core.llm.provider import Message
    from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
    from monkeybot.core.types.content_blocks import Text, Thinking

    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    try:
        hist = backend.history()
        await hist.append(
            "think-session",
            Message(role="user", content=[Text(text="why?")]),
        )
        await hist.append(
            "think-session",
            Message(
                role="assistant",
                content=[
                    Thinking(thinking="weigh options", signature="sig"),
                    Text(text="because"),
                ],
            ),
        )

        app = create_app(loop_port=FakeLoopPort(registry), registry=registry)
        app.state.storage = backend
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            rd = await client.get("/api/chat-history/think-session")
            assert rd.status_code == 200
            assert rd.json()["messages"] == [
                {"role": "user", "text": "why?"},
                {"role": "thinking", "text": "weigh options"},
                {"role": "assistant", "text": "because"},
            ]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_chat_history_disabled_returns_404(registry: SessionRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONKEYBOT_CHAT_HISTORY_API", "0")
    app = create_app(loop_port=FakeLoopPort(registry), registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for method, path in (
            (client.get, "/api/chat-history"),
            (client.delete, "/api/chat-history/session-a"),
        ):
            r = await method(path)
            assert r.status_code == 404
            assert r.json()["error"]["code"] == "NOT_FOUND"
