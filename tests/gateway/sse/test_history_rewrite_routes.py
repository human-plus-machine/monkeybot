"""Gateway routes for edit, regenerate, rewind, truncate, and fork."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from monkeybot.core.llm.provider import Message
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.types.content_blocks import ContentBlock, Text
from monkeybot.gateway.sse.routes import create_app
from monkeybot.gateway.sse.session_bus import SessionRegistry


class RecordingLoop:
    """Records the turn the edit/regenerate handoff starts, then goes idle."""

    def __init__(self, registry: SessionRegistry) -> None:
        self._registry = registry
        self.turns: list[tuple[str, str, list[ContentBlock]]] = []

    async def start_turn(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> None:
        self.turns.append((session_id, request_id, list(user_content)))
        bus = self._registry.get(session_id)
        if bus is not None and bus.current_request_id == request_id:
            bus.current_request_id = None


class HoldingLoop:
    def __init__(self) -> None:
        self.started = False

    async def start_turn(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> None:
        _ = (session_id, request_id, user_content)
        self.started = True
        await asyncio.Event().wait()


async def _seed(backend: SQLiteStorageBackend, session_id: str) -> None:
    history = backend.history()
    await history.append(session_id, Message(role="user", content=[Text(text="hello")]))
    await history.append(session_id, Message(role="assistant", content=[Text(text="hi")]))
    await history.append(session_id, Message(role="user", content=[Text(text="again")]))
    await history.append(session_id, Message(role="assistant", content=[Text(text="ok")]))


@pytest.fixture
async def backend() -> AsyncIterator[SQLiteStorageBackend]:
    store = SQLiteStorageBackend("sqlite:///:memory:")
    await store.open()
    try:
        yield store
    finally:
        await store.close()


def _app(registry: SessionRegistry, backend: SQLiteStorageBackend, loop: object):
    app = create_app(loop_port=loop, registry=registry)
    app.state.storage = backend
    return app


async def _anchor(client: AsyncClient, session_id: str, text: str) -> dict[str, object]:
    detail = await client.get(f"/api/chat-history/{session_id}")
    assert detail.status_code == 200
    for row in detail.json()["messages"]:
        if row.get("text") == text:
            return row["anchor"]
    raise AssertionError(f"no row with text {text!r}")


@pytest.mark.asyncio
async def test_edit_hands_off_turn_and_keeps_root(
    backend: SQLiteStorageBackend,
) -> None:
    registry = SessionRegistry()
    loop = RecordingLoop(registry)
    app = _app(registry, backend, loop)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/sessions", json={"session_id": "sess-edit"})
        assert created.status_code == 201
        await _seed(backend, "sess-edit")
        anchor = await _anchor(client, "sess-edit", "again")
        edited = await client.post(
            "/sessions/sess-edit/branches",
            json={
                "op": "edit",
                "anchor": anchor,
                "message": "revised",
                "request_id": "req-edit",
            },
        )
        assert edited.status_code == 200
        await asyncio.sleep(0)
        body = edited.json()
        assert body["request_id"] == "req-edit"
        assert body["branch_id"] != "root"
        assert loop.turns
        assert loop.turns[0][0] == "sess-edit"
        assert loop.turns[0][1] == "req-edit"
        assert loop.turns[0][2] == [Text(text="revised")]

        root = await backend.history().load("sess-edit")
        assert [block.text for msg in root for block in msg.content if isinstance(block, Text)] == [
            "hello",
            "hi",
            "again",
            "ok",
        ]
        detail = await client.get("/api/chat-history/sess-edit")
        assert detail.json()["branch_id"] == body["branch_id"]
        assert [row["text"] for row in detail.json()["messages"]] == ["hello", "hi"]
        listed = await client.get("/api/chat-history")
        assert listed.status_code == 200
        summary = next(
            item for item in listed.json()["threads"] if item["session_id"] == "sess-edit"
        )
        assert summary["preview"] == "hi"
        assert summary["message_count"] == 2
        bus = registry.get("sess-edit")
        assert bus is not None
        frames = [frame for _seq, frame in bus._replay_primary]
        assert any("HistoryRewritten" in frame and "edit" in frame for frame in frames)


@pytest.mark.asyncio
async def test_branch_op_rejects_busy_session(backend: SQLiteStorageBackend) -> None:
    registry = SessionRegistry()
    app = _app(registry, backend, HoldingLoop())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/sessions", json={"session_id": "sess-busy"})
        await _seed(backend, "sess-busy")
        anchor = await _anchor(client, "sess-busy", "hello")
        held = await client.post(
            "/sessions/sess-busy/reply",
            json={"request_id": "req-hold", "message": "working"},
        )
        assert held.status_code == 200
        busy = await client.post(
            "/sessions/sess-busy/branches",
            json={"op": "rewind", "anchor": anchor, "request_id": "req-rewind"},
        )
        assert busy.status_code == 409
        assert busy.json()["error"]["code"] == "SESSION_BUSY"
        bus = registry.get("sess-busy")
        task = bus.active_turn_task if bus is not None else None
        if task is not None:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


class _LiveVoice:
    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    def get(self, session_id: str) -> object | None:
        return object() if session_id == self._session_id else None


@pytest.mark.asyncio
async def test_rewrites_reject_a_live_voice_session(backend: SQLiteStorageBackend) -> None:
    registry = SessionRegistry()
    app = _app(registry, backend, RecordingLoop(registry))
    app.state.realtime_manager = _LiveVoice("sess-voice")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/sessions", json={"session_id": "sess-voice"})
        await _seed(backend, "sess-voice")
        anchor = await _anchor(client, "sess-voice", "again")
        attempts = [
            client.post("/sessions/sess-voice/branches", json={"op": "rewind", "anchor": anchor}),
            client.post("/sessions/sess-voice/truncate", json={"anchor": anchor}),
            client.post("/sessions/sess-voice/fork", json={"anchor": anchor}),
            client.put("/sessions/sess-voice/branches/active", json={"branch_id": "root"}),
        ]
        for attempt in attempts:
            response = await attempt
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "SESSION_BUSY"
        assert await backend.branches().list("sess-voice") == []
        assert len(await backend.history().load("sess-voice")) == 4
        assert await backend.session_turns().try_acquire("sess-voice", "after")


@pytest.mark.asyncio
async def test_fork_creates_a_new_session(backend: SQLiteStorageBackend) -> None:
    registry = SessionRegistry()
    app = _app(registry, backend, RecordingLoop(registry))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/sessions", json={"session_id": "sess-fork"})
        await _seed(backend, "sess-fork")
        anchor = await _anchor(client, "sess-fork", "hello")
        forked = await client.post(
            "/sessions/sess-fork/fork",
            json={"anchor": anchor},
        )
        assert forked.status_code == 200
        new_id = forked.json()["session_id"]
        assert new_id != "sess-fork"
        copied = await backend.history().load(new_id)
        assert len(copied) == 1
        assert copied[0].content[0].text == "hello"  # type: ignore[union-attr]
        assert len(await backend.history().load("sess-fork")) == 4


@pytest.mark.asyncio
async def test_delete_chat_history_cascades_to_branches(
    backend: SQLiteStorageBackend,
) -> None:
    registry = SessionRegistry()
    app = _app(registry, backend, RecordingLoop(registry))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/sessions", json={"session_id": "sess-del"})
        await _seed(backend, "sess-del")
        anchor = await _anchor(client, "sess-del", "again")
        edited = await client.post(
            "/sessions/sess-del/branches",
            json={"op": "rewind", "anchor": anchor},
        )
        assert edited.status_code == 200
        branch_id = edited.json()["branch_id"]
        records = await backend.branches().list("sess-del")
        child = next(row for row in records if row.branch_id == branch_id)
        assert await backend.history().load(child.thread_id)

        deleted = await client.delete("/api/chat-history/sess-del")
        assert deleted.status_code == 200
        assert await backend.history().load("sess-del") == []
        assert await backend.history().load(child.thread_id) == []
        assert await backend.branches().list("sess-del") == []


@pytest.mark.asyncio
async def test_ending_a_session_keeps_its_branches(backend: SQLiteStorageBackend) -> None:
    registry = SessionRegistry()
    app = _app(registry, backend, RecordingLoop(registry))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/sessions", json={"session_id": "sess-end"})
        await _seed(backend, "sess-end")
        anchor = await _anchor(client, "sess-end", "again")
        edited = await client.post(
            "/sessions/sess-end/branches",
            json={"op": "rewind", "anchor": anchor},
        )
        assert edited.status_code == 200
        branch_id = edited.json()["branch_id"]
        child = await backend.branches().get("sess-end", branch_id)
        assert child is not None
        kept = await backend.history().load(child.thread_id)

        ended = await client.delete("/sessions/sess-end")
        assert ended.status_code == 200
        assert await backend.history().load(child.thread_id) == kept
        active = await backend.branches().get_active("sess-end")
        assert active is not None and active.branch_id == branch_id


@pytest.mark.asyncio
async def test_switch_branch_reloads_that_transcript(backend: SQLiteStorageBackend) -> None:
    registry = SessionRegistry()
    app = _app(registry, backend, RecordingLoop(registry))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/sessions", json={"session_id": "sess-sw"})
        await _seed(backend, "sess-sw")
        anchor = await _anchor(client, "sess-sw", "again")
        edited = await client.post(
            "/sessions/sess-sw/branches",
            json={"op": "rewind", "anchor": anchor},
        )
        assert edited.status_code == 200
        switched = await client.put(
            "/sessions/sess-sw/branches/active",
            json={"branch_id": "root"},
        )
        assert switched.status_code == 200
        detail = await client.get("/api/chat-history/sess-sw")
        texts = [row["text"] for row in detail.json()["messages"]]
        assert texts == ["hello", "hi", "again", "ok"]
        assert detail.json()["branch_id"] == "root"
        points = detail.json()["branch_points"]
        assert points
        assert "root" in points[0]["options"]
        assert edited.json()["branch_id"] in points[0]["options"]


def test_history_rewritten_roundtrip() -> None:
    from monkeybot.core.runtime.events import HistoryRewritten, event_from_json, event_to_json

    event = HistoryRewritten(
        request_id="r",
        session_id="s",
        branch_id="b",
        op="rewind",
    )
    assert event_from_json(event_to_json(event)) == event
    payload = json.loads(event_to_json(event))
    assert payload["type"] == "HistoryRewritten"
