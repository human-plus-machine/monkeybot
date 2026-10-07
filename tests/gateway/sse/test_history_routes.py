"""Branch routes, chat-history branch views, delete purge, and voice-call blocking."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from monkeybot.core.config.realtime_config import RealtimeConfig
from monkeybot.core.llm.provider import Message
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.persistence.thread_summary import CONTEXT_SUMMARY_PREFIX
from monkeybot.core.runtime.history_rewrite import resolve_active_thread_id
from monkeybot.core.types.content_blocks import ContentBlock, Text
from monkeybot.gateway.realtime.manager import RealtimeSessionManager
from monkeybot.gateway.sse.routes import create_app
from monkeybot.gateway.sse.session_bus import SessionRegistry

SESSION = "s1"


class _ThreadWritingLoop:
    """Writes the user turn and a canned reply to the session's active thread."""

    def __init__(self, registry: SessionRegistry, backend: SQLiteStorageBackend) -> None:
        self._registry = registry
        self._backend = backend
        self.turns: list[tuple[str, list[ContentBlock]]] = []

    async def start_turn(
        self, session_id: str, request_id: str, user_content: list[ContentBlock]
    ) -> None:
        thread = await resolve_active_thread_id(self._backend, session_id)
        self.turns.append((thread, user_content))
        history = self._backend.history()
        await history.append(thread, Message(role="user", content=list(user_content)))
        await history.append(thread, Message(role="assistant", content=[Text(text="reply")]))


@pytest_asyncio.fixture
async def backend() -> AsyncIterator[SQLiteStorageBackend]:
    storage = SQLiteStorageBackend("sqlite:///:memory:")
    await storage.open()
    yield storage
    await storage.close()


@pytest_asyncio.fixture
async def harness(backend: SQLiteStorageBackend) -> AsyncIterator[dict[str, Any]]:
    registry = SessionRegistry()
    registry.create(SESSION, agent_md=None, created_at_ms=0)
    loop = _ThreadWritingLoop(registry, backend)
    app = create_app(loop_port=loop, registry=registry)
    app.state.storage = backend
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"client": client, "app": app, "loop": loop, "registry": registry}


async def _seed(backend: SQLiteStorageBackend, *texts: str, thread: str = SESSION) -> None:
    for index, text in enumerate(texts):
        role = "user" if index % 2 == 0 else "assistant"
        await backend.history().append(thread, Message(role=role, content=[Text(text=text)]))  # type: ignore[arg-type]


async def _detail(client: AsyncClient, session_id: str = SESSION) -> dict[str, Any]:
    response = await client.get(f"/api/chat-history/{session_id}")
    assert response.status_code == 200
    return response.json()


async def _wait_turn(harness: dict[str, Any]) -> None:
    task = harness["registry"].get(SESSION).active_turn_task
    if task is not None:
        await task


@pytest.mark.asyncio
async def test_edit_branches_and_replays_into_the_new_thread(harness, backend) -> None:
    client = harness["client"]
    await _seed(backend, "u1", "a1", "u2", "a2")
    rows = (await _detail(client))["messages"]
    response = await client.post(
        f"/sessions/{SESSION}/branches",
        json={"op": "edit", "anchor": rows[2]["anchor"], "message": "u2 edited"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request_id"]
    await _wait_turn(harness)

    [(thread, content)] = harness["loop"].turns
    assert thread.endswith(body["branch_id"])
    assert [b.text for b in content] == ["u2 edited"]  # type: ignore[attr-defined]
    detail = await _detail(client)
    assert detail["branch_id"] == body["branch_id"]
    assert [m["text"] for m in detail["messages"]] == ["u1", "a1", "u2 edited", "reply"]
    [point] = detail["branch_points"]
    assert point["anchor"] == detail["messages"][2]["anchor"]
    assert point["options"] == ["root", body["branch_id"]]
    assert point["active_index"] == 1

    listed = (await client.get("/api/chat-history")).json()["threads"]
    assert [t["session_id"] for t in listed] == [SESSION]
    assert listed[0]["preview"] == "reply"


@pytest.mark.asyncio
async def test_restore_drops_the_user_turn_without_replying(harness, backend) -> None:
    client = harness["client"]
    await _seed(backend, "keep", "kept reply", "redo me", "redo reply")
    rows = (await _detail(client))["messages"]
    created = await client.post(
        f"/sessions/{SESSION}/branches",
        json={"op": "restore", "anchor": rows[2]["anchor"]},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["request_id"] is None
    assert [m["text"] for m in (await _detail(client))["messages"]] == ["keep", "kept reply"]
    assert harness["loop"].turns == []

    await client.put(f"/sessions/{SESSION}/branches/active", json={"branch_id": "root"})
    opened = (await _detail(client))["messages"]
    first = await client.post(
        f"/sessions/{SESSION}/branches",
        json={"op": "restore", "anchor": opened[0]["anchor"]},
    )
    assert first.status_code == 422, first.text
    with_text = await client.post(
        f"/sessions/{SESSION}/branches",
        json={"op": "restore", "anchor": opened[2]["anchor"], "message": "lost"},
    )
    assert with_text.status_code == 400, with_text.text
    assert len((await _detail(client))["messages"]) == 4
    with_id = await client.post(
        f"/sessions/{SESSION}/branches",
        json={"op": "restore", "anchor": opened[2]["anchor"], "request_id": "r1"},
    )
    assert with_id.status_code == 200, with_id.text
    assert with_id.json()["request_id"] is None
    assert harness["loop"].turns == []


@pytest.mark.asyncio
async def test_restore_drains_follow_ups_queued_during_the_op(harness, backend) -> None:
    client = harness["client"]
    await _seed(backend, "keep", "kept reply", "redo me", "redo reply")
    rows = (await _detail(client))["messages"]
    # The state POST /queue leaves when it meets the restore's lease.
    harness["registry"].get(SESSION).admission.enqueue_follow_up("q1", [Text(text="queued")])
    created = await client.post(
        f"/sessions/{SESSION}/branches",
        json={"op": "restore", "anchor": rows[2]["anchor"]},
    )
    assert created.status_code == 200, created.text
    await _wait_turn(harness)
    [(thread, content)] = harness["loop"].turns
    assert thread.endswith(created.json()["branch_id"])
    assert [b.text for b in content] == ["queued"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_summarized_rows_are_read_only_over_http(harness, backend) -> None:
    client = harness["client"]
    await _seed(backend, "u1", f"{CONTEXT_SUMMARY_PREFIX}\nfolded", "u2", "a2")
    rows = (await _detail(client))["messages"]
    assert [m["rewritable"] for m in rows] == [False, False, True, True]
    assert [m["rewindable"] for m in rows] == [False, True, True, True]
    for op, row in (("rewind", rows[0]), ("regenerate", rows[1])):
        response = await client.post(
            f"/sessions/{SESSION}/branches", json={"op": op, "anchor": row["anchor"]}
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "SUMMARIZED"
    truncated = await client.post(
        f"/sessions/{SESSION}/truncate", json={"anchor": rows[0]["anchor"]}
    )
    assert truncated.status_code == 422, truncated.text
    assert truncated.json()["error"]["code"] == "SUMMARIZED"
    assert len((await _detail(client))["messages"]) == 4


@pytest.mark.asyncio
async def test_switch_back_to_root_and_list_branches(harness, backend) -> None:
    client = harness["client"]
    await _seed(backend, "u1", "a1")
    rows = (await _detail(client))["messages"]
    created = await client.post(
        f"/sessions/{SESSION}/branches", json={"op": "rewind", "anchor": rows[0]["anchor"]}
    )
    assert created.status_code == 200, created.text
    assert created.json()["request_id"] is None
    assert [m["text"] for m in (await _detail(client))["messages"]] == ["u1"]

    switched = await client.put(f"/sessions/{SESSION}/branches/active", json={"branch_id": "root"})
    assert switched.status_code == 200
    assert [m["text"] for m in (await _detail(client))["messages"]] == ["u1", "a1"]

    listing = (await client.get(f"/sessions/{SESSION}/branches")).json()
    assert listing["active_branch_id"] == "root"
    assert {b["branch_id"] for b in listing["branches"]} == {"root", created.json()["branch_id"]}

    missing = await client.put(f"/sessions/{SESSION}/branches/active", json={"branch_id": "nope"})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "BRANCH_NOT_FOUND"


@pytest.mark.asyncio
async def test_unbranched_session_lists_implicit_root(harness) -> None:
    listing = (await harness["client"].get(f"/sessions/{SESSION}/branches")).json()
    assert listing["active_branch_id"] == "root"
    assert [b["branch_id"] for b in listing["branches"]] == ["root"]


@pytest.mark.asyncio
async def test_bad_anchor_and_boundary_errors(harness, backend) -> None:
    client = harness["client"]
    await _seed(backend, "u1", "a1")
    rows = (await _detail(client))["messages"]
    stale = await client.post(
        f"/sessions/{SESSION}/branches", json={"op": "rewind", "anchor": {"row_id": "gone"}}
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "ANCHOR_MISMATCH"
    not_user = await client.post(
        f"/sessions/{SESSION}/branches",
        json={"op": "edit", "anchor": rows[1]["anchor"], "message": "x"},
    )
    assert not_user.status_code == 422
    no_text = await client.post(
        f"/sessions/{SESSION}/branches", json={"op": "edit", "anchor": rows[0]["anchor"]}
    )
    assert no_text.status_code == 400
    # A failed op releases the turn lock.
    assert await backend.session_turns().try_acquire(SESSION, "probe")


@pytest.mark.asyncio
async def test_busy_session_and_live_voice_call_are_rejected(harness, backend) -> None:
    client = harness["client"]
    await _seed(backend, "u1", "a1")
    anchor = (await _detail(client))["messages"][0]["anchor"]
    assert await backend.session_turns().try_acquire(SESSION, "other-turn")
    busy = await client.post(
        f"/sessions/{SESSION}/branches", json={"op": "rewind", "anchor": anchor}
    )
    assert busy.status_code == 409
    await backend.session_turns().release(SESSION, "other-turn")

    manager = RealtimeSessionManager(RealtimeConfig())
    assert manager.claim(SESSION)
    harness["app"].state.realtime_manager = manager
    voice = await client.post(
        f"/sessions/{SESSION}/branches", json={"op": "rewind", "anchor": anchor}
    )
    assert voice.status_code == 409
    assert voice.json()["error"]["code"] == "SESSION_BUSY"
    assert (await client.delete(f"/api/chat-history/{SESSION}")).status_code == 409
    assert len(await backend.history().load(SESSION)) == 2

    manager.unclaim(SESSION)
    ok = await client.post(f"/sessions/{SESSION}/branches", json={"op": "rewind", "anchor": anchor})
    assert ok.status_code == 200
    # The rewrite block is lifted once the op finishes, so a call can connect again.
    assert manager.claim(SESSION)


def test_voice_claim_and_rewrite_block_exclude_each_other() -> None:
    manager = RealtimeSessionManager(RealtimeConfig())
    assert manager.begin_rewrite(SESSION)
    assert not manager.claim(SESSION)
    assert not manager.begin_rewrite(SESSION)
    manager.end_rewrite(SESSION)
    assert manager.claim(SESSION)
    assert not manager.begin_rewrite(SESSION)
    assert not manager.claim(SESSION)
    manager.unclaim(SESSION)
    assert manager.begin_rewrite(SESSION)


@pytest.mark.asyncio
async def test_delete_purges_branch_threads(harness, backend) -> None:
    client = harness["client"]
    await _seed(backend, "u1", "a1")
    anchor = (await _detail(client))["messages"][0]["anchor"]
    created = await client.post(
        f"/sessions/{SESSION}/branches", json={"op": "rewind", "anchor": anchor}
    )
    branch_thread = (await backend.branches().get_active(SESSION)).thread_id  # type: ignore[union-attr]
    assert created.status_code == 200
    deleted = await client.delete(f"/api/chat-history/{SESSION}")
    assert deleted.json() == {"deleted": True}
    assert await backend.history().load(branch_thread) == []
    assert await backend.history().load(SESSION) == []
    assert await backend.branches().list(SESSION) == []


@pytest.mark.asyncio
async def test_continue_picks_the_session_with_newest_branch_activity(harness, backend) -> None:
    """``--continue`` resumes the first listed thread; branch activity must count."""
    client = harness["client"]
    await _seed(backend, "u1", "a1")
    await _seed(backend, "other", "reply", thread="s0-newer")
    assert (await client.get("/api/chat-history")).json()["threads"][0]["session_id"] == "s0-newer"
    anchor = (await _detail(client))["messages"][0]["anchor"]
    await client.post(
        f"/sessions/{SESSION}/branches", json={"op": "edit", "anchor": anchor, "message": "again"}
    )
    await _wait_turn(harness)
    threads = (await client.get("/api/chat-history")).json()["threads"]
    assert [t["session_id"] for t in threads] == [SESSION, "s0-newer"]


@pytest.mark.asyncio
async def test_truncate_shortens_the_active_branch_unless_it_strands_one(harness, backend) -> None:
    client = harness["client"]
    await _seed(backend, "u1", "a1", "u2", "a2", "u3", "a3")
    rows = (await _detail(client))["messages"]
    response = await client.post(
        f"/sessions/{SESSION}/truncate", json={"anchor": rows[3]["anchor"]}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"branch_id": "root"}
    assert [m["text"] for m in (await _detail(client))["messages"]] == ["u1", "a1", "u2", "a2"]

    edited = await client.post(
        f"/sessions/{SESSION}/branches",
        json={"op": "edit", "anchor": rows[2]["anchor"], "message": "v2"},
    )
    await _wait_turn(harness)
    await client.put(f"/sessions/{SESSION}/branches/active", json={"branch_id": "root"})
    stranding = await client.post(
        f"/sessions/{SESSION}/truncate", json={"anchor": rows[0]["anchor"]}
    )
    assert stranding.status_code == 409
    assert stranding.json()["error"]["code"] == "BRANCHES_IN_TAIL"
    assert edited.json()["branch_id"] in (await _detail(client))["branch_points"][0]["options"]
    assert await backend.session_turns().try_acquire(SESSION, "probe")


@pytest.mark.asyncio
async def test_fork_starts_a_new_session_from_the_active_branch(harness, backend) -> None:
    client = harness["client"]
    await _seed(backend, "u1", "a1", "u2", "a2")
    rows = (await _detail(client))["messages"]
    response = await client.post(f"/sessions/{SESSION}/fork", json={"anchor": rows[1]["anchor"]})
    assert response.status_code == 200, response.text
    forked = response.json()["session_id"]
    assert forked != SESSION
    assert [m["text"] for m in (await _detail(client, forked))["messages"]] == ["u1", "a1"]
    assert len((await _detail(client))["messages"]) == 4
    listed = {t["session_id"] for t in (await client.get("/api/chat-history")).json()["threads"]}
    assert listed == {SESSION, forked}

    stale = await client.post(f"/sessions/{SESSION}/fork", json={"anchor": {"row_id": "gone"}})
    assert stale.status_code == 409
