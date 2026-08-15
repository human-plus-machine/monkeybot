"""MemPalace outbox, writer, and wake-up tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import Message
from monkeybot.core.memory.ids import conversation_wing, outbox_id
from monkeybot.core.memory.ingest import persist_message, visible_text
from monkeybot.core.memory.outbox import ensure_outbox_schema, insert_pending
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.persistence.sqlite import apply_schema, open_connection
from monkeybot.core.types.content_blocks import Text, Thinking
from tests.core.memory.in_memory_palace import InMemoryPalace


@pytest.fixture
async def db_url(tmp_path: Path) -> str:
    path = tmp_path / "monkeybot.db"
    url = f"sqlite:///{path}"
    conn = await open_connection(url)
    await apply_schema(conn)
    await ensure_outbox_schema(conn)
    await conn.close()
    return url


def _subsystem(tmp_path: Path, db_url: str, *, agent_id: str = "agent-a") -> MemorySubsystem:
    palace = InMemoryPalace(tmp_path / agent_id / "mempalace", agent_name=agent_id)
    return MemorySubsystem(
        memory_uri=f"local://{palace.palace_path}",
        db_url=db_url,
        agent_id=agent_id,
        agent_name=agent_id,
        palace=palace,
    )


def test_outbox_id_is_deterministic() -> None:
    a = outbox_id(agent_id="a", thread_id="t", message_id="m", role="user")
    b = outbox_id(agent_id="a", thread_id="t", message_id="m", role="user")
    c = outbox_id(agent_id="a", thread_id="t", message_id="m", role="assistant")
    assert a == b
    assert a != c
    assert a.startswith("turn_")


def test_visible_text_skips_thinking() -> None:
    msg = Message(
        role="assistant",
        content=[Thinking(thinking="secret"), Text(text="Hello there")],
    )
    assert visible_text(msg) == "Hello there"


def test_conversation_wing() -> None:
    assert conversation_wing(None) == "main"
    assert conversation_wing("ws-1") == "ws-1"


@pytest.mark.asyncio
async def test_outbox_insert_is_idempotent_for_committed(db_url: str, tmp_path: Path) -> None:
    conn = await open_connection(db_url)
    await ensure_outbox_schema(conn)
    first = await insert_pending(
        conn,
        agent_id="a",
        thread_id="t",
        turn_id="turn",
        message_id="m1",
        role="user",
        content="hi",
        workspace_id=None,
        wing="main",
        room="conversation",
    )
    assert first is not None
    from monkeybot.core.memory.outbox import mark_committed

    await mark_committed(conn, [first])
    again = await insert_pending(
        conn,
        agent_id="a",
        thread_id="t",
        turn_id="turn",
        message_id="m1",
        role="user",
        content="hi",
        workspace_id=None,
        wing="main",
        room="conversation",
    )
    await conn.close()
    assert again is None


@pytest.mark.asyncio
async def test_writer_upserts_and_is_idempotent(db_url: str, tmp_path: Path) -> None:
    sub = _subsystem(tmp_path, db_url)
    await sub.enqueue(
        thread_id="t1",
        turn_id="turn1",
        message_id="m1",
        role="user",
        content="I prefer concise answers.",
    )
    n = await sub.drain_writer(timeout_s=2)
    assert n == 1
    drawer_id = outbox_id(agent_id="agent-a", thread_id="t1", message_id="m1", role="user")
    got = await sub.get_drawer(drawer_id)
    assert got is not None
    assert "concise answers" in got["content"]
    n2 = await sub.drain_writer(timeout_s=1)
    assert n2 == 0
    await sub.close()


@pytest.mark.asyncio
async def test_wake_up_and_recall(db_url: str, tmp_path: Path) -> None:
    sub = _subsystem(tmp_path, db_url)
    await sub.ensure_ready()
    await sub.enqueue(
        thread_id="t1",
        turn_id="turn1",
        message_id="m1",
        role="user",
        content="Remember the sky is blue.",
    )
    await sub.drain_writer(timeout_s=2)
    lines = await sub.load_index()
    assert any("IDENTITY" in ln or "sky is blue" in ln for ln in lines)
    drawers = await sub.recall(wing="main", room="conversation")
    assert drawers
    assert drawers[0].content == "Remember the sky is blue."
    await sub.close()


@pytest.mark.asyncio
async def test_two_agents_isolated(tmp_path: Path) -> None:
    db_a = f"sqlite:///{tmp_path / 'a.db'}"
    db_b = f"sqlite:///{tmp_path / 'b.db'}"
    for url in (db_a, db_b):
        conn = await open_connection(url)
        await apply_schema(conn)
        await conn.close()
    a = _subsystem(tmp_path, db_a, agent_id="agent-a")
    b = _subsystem(tmp_path, db_b, agent_id="agent-b")
    await a.ensure_ready()
    await b.ensure_ready()
    await a.enqueue(
        thread_id="t",
        turn_id="1",
        message_id="m",
        role="user",
        content="secret for A",
    )
    await b.enqueue(
        thread_id="t",
        turn_id="1",
        message_id="m",
        role="user",
        content="secret for B",
    )
    await asyncio.gather(a.drain_writer(timeout_s=2), b.drain_writer(timeout_s=2))
    a_lines = "\n".join(await a.load_index())
    b_lines = "\n".join(await b.load_index())
    assert "secret for A" in a_lines
    assert "secret for B" not in a_lines
    assert "secret for B" in b_lines
    assert "secret for A" not in b_lines
    await a.close()
    await b.close()


@pytest.mark.asyncio
async def test_pre_turn_injects_l2(db_url: str, tmp_path: Path) -> None:
    from monkeybot.core.context import TurnContext

    sub = _subsystem(tmp_path, db_url)
    await sub.ensure_ready()
    await sub.enqueue(
        thread_id="t1",
        turn_id="turn1",
        message_id="m1",
        role="user",
        content="User likes dark mode.",
    )
    await sub.drain_writer(timeout_s=2)
    mgr = HookManager()
    sub.register_hooks(mgr)
    ctx = TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="md",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
        memory=sub,
    )
    payload = HookPayload(
        event=HookEvent.PRE_TURN,
        thread_id="t1",
        request_id="r1",
        ctx=ctx,
        user_message="what theme?",
    )
    await sub._hook.on_pre_turn(payload)
    assert payload.inject_memory_lines
    assert any("dark mode" in ln for ln in payload.inject_memory_lines)
    await sub.close()


@pytest.mark.asyncio
async def test_history_append_enqueues(db_url: str, tmp_path: Path) -> None:
    from monkeybot.core.context import TurnContext
    from monkeybot.core.persistence.history import SQLiteHistoryStore

    conn = await open_connection(db_url)
    history = SQLiteHistoryStore(conn)
    sub = _subsystem(tmp_path, db_url)
    await sub.ensure_ready()
    ctx = TurnContext(
        thread_id="t1",
        request_id="turn-1",
        agent_md="md",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
        memory=sub,
    )
    await persist_message(
        history,
        Message(role="user", content=[Text(text="hello palace")]),
        thread_id=ctx.thread_id,
        turn_id=ctx.request_id,
        memory=sub,
        ingest=True,
    )
    await sub.drain_writer(timeout_s=2)
    lines = "\n".join(await sub.load_index())
    assert "hello palace" in lines
    await sub.close()
    await conn.close()


def test_palace_uri_rejects_object_store() -> None:
    from monkeybot.core.memory.palace import palace_path_from_uri

    with pytest.raises(ValueError, match="does not support gcs://"):
        palace_path_from_uri("gcs://bucket/prefix")
    with pytest.raises(ValueError, match="does not support s3://"):
        palace_path_from_uri("s3://bucket/prefix")


def test_layout_falls_back_from_object_store_uri(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from monkeybot.core.layout import AgentLayout, resolve_memory_storage_uri

    uri = resolve_memory_storage_uri("gcs://bucket/mem", tmp_path)
    assert uri.startswith("local://")
    assert uri.endswith("memory/mempalace")
    monkeypatch.setenv("MEMORY_STORAGE_URI", "s3://bucket/mem")
    layout = AgentLayout.from_environment(agent_root=tmp_path)
    assert layout.memory_storage_uri.startswith("local://")


def test_create_palace_requires_memory_extra(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from monkeybot.core.memory import palace as palace_mod

    monkeypatch.setattr(palace_mod, "mempalace_available", lambda: False)
    with pytest.raises(palace_mod.MemoryDependencyError, match="monkeybot\\[memory\\]"):
        palace_mod.create_palace(f"local://{tmp_path / 'p'}", agent_name="a")


@pytest.mark.asyncio
async def test_recall_is_thread_scoped(db_url: str, tmp_path: Path) -> None:
    sub = _subsystem(tmp_path, db_url)
    await sub.ensure_ready()
    await sub.enqueue(
        thread_id="secret",
        turn_id="1",
        message_id="m1",
        role="user",
        content="private other thread",
    )
    await sub.enqueue(
        thread_id="t1",
        turn_id="1",
        message_id="m2",
        role="user",
        content="this thread only",
    )
    await sub.drain_writer(timeout_s=2)
    drawers = await sub.recall(wing="main", room="conversation", thread_id="t1")
    texts = [d.content for d in drawers]
    assert "this thread only" in texts
    assert "private other thread" not in texts
    await sub.close()


@pytest.mark.asyncio
async def test_poison_row_does_not_dead_letter_siblings(db_url: str, tmp_path: Path) -> None:
    from monkeybot.core.memory.outbox import STATUS_COMMITTED, STATUS_PENDING

    palace = InMemoryPalace(tmp_path / "poison" / "mempalace", agent_name="agent-a")
    original = palace.upsert_drawer

    def flaky(drawer_id: str, content: str, metadata: dict[str, str]) -> None:
        if "bad" in content:
            raise RuntimeError("poison")
        original(drawer_id, content, metadata)

    palace.upsert_drawer = flaky  # type: ignore[method-assign]
    sub = MemorySubsystem(
        memory_uri=f"local://{palace.palace_path}",
        db_url=db_url,
        agent_id="agent-a",
        agent_name="agent-a",
        palace=palace,
    )
    await sub.enqueue(
        thread_id="t",
        turn_id="1",
        message_id="bad",
        role="user",
        content="bad drawer",
    )
    await sub.enqueue(
        thread_id="t",
        turn_id="1",
        message_id="ok",
        role="user",
        content="healthy drawer",
    )
    await sub.drain_writer(timeout_s=2)
    conn = await open_connection(db_url)
    cur = await conn.execute("SELECT message_id, status FROM memory_outbox ORDER BY message_id")
    rows = {str(r[0]): str(r[1]) for r in await cur.fetchall()}
    await cur.close()
    await conn.close()
    assert rows["ok"] == STATUS_COMMITTED
    assert rows["bad"] == STATUS_PENDING
    got = await sub.get_drawer(
        outbox_id(agent_id="agent-a", thread_id="t", message_id="ok", role="user")
    )
    assert got is not None
    assert "healthy" in got["content"]
    await sub.close()


@pytest.mark.asyncio
async def test_history_survives_outbox_failure(db_url: str, tmp_path: Path) -> None:
    from monkeybot.core.persistence.history import SQLiteHistoryStore

    conn = await open_connection(db_url)
    history = SQLiteHistoryStore(conn)

    class Boom:
        async def append_with_outbox(self, *args: object, **kwargs: object) -> None:
            raise TimeoutError("outbox down")

        async def append(self, thread_id: str, message: Message, **kwargs: object) -> None:
            await history.append(thread_id, message, **kwargs)

    sub = _subsystem(tmp_path, db_url)
    await persist_message(
        Boom(),  # type: ignore[arg-type]
        Message(role="user", content=[Text(text="keep me")]),
        thread_id="t1",
        turn_id="turn-1",
        memory=sub,
        ingest=True,
    )
    loaded = await history.load("t1")
    assert loaded
    assert visible_text(loaded[0]) == "keep me"
    await conn.close()
    await sub.close()


@pytest.mark.asyncio
async def test_claim_batch_filters_by_agent(db_url: str) -> None:
    from monkeybot.core.memory.outbox import claim_batch, insert_pending

    conn = await open_connection(db_url)
    await ensure_outbox_schema(conn)
    await insert_pending(
        conn,
        agent_id="agent-a",
        thread_id="t",
        turn_id="1",
        message_id="ma",
        role="user",
        content="for a",
        workspace_id=None,
        wing="main",
        room="conversation",
    )
    await insert_pending(
        conn,
        agent_id="agent-b",
        thread_id="t",
        turn_id="1",
        message_id="mb",
        role="user",
        content="for b",
        workspace_id=None,
        wing="main",
        room="conversation",
    )
    claimed = await claim_batch(conn, lease_owner="w1", agent_id="agent-a")
    await conn.close()
    assert [row.content for row in claimed] == ["for a"]


@pytest.mark.asyncio
async def test_gc_committed_deletes_old_rows(db_url: str) -> None:
    from datetime import UTC, datetime, timedelta

    from monkeybot.core.memory.outbox import STATUS_COMMITTED, gc_committed

    conn = await open_connection(db_url)
    await ensure_outbox_schema(conn)
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat(timespec="seconds")
    await conn.execute(
        """
        INSERT INTO memory_outbox (
            id, agent_id, thread_id, turn_id, message_id, role, content,
            workspace_id, wing, room, created_at, status, attempts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "old-row",
            "a",
            "t",
            "1",
            "m",
            "user",
            "gone",
            None,
            "main",
            "conversation",
            old,
            STATUS_COMMITTED,
            1,
        ),
    )
    await conn.commit()
    deleted = await gc_committed(conn, days=7)
    cur = await conn.execute("SELECT COUNT(*) FROM memory_outbox WHERE id = 'old-row'")
    remaining = (await cur.fetchone())[0]
    await cur.close()
    await conn.close()
    assert deleted == 1
    assert remaining == 0


@pytest.mark.asyncio
async def test_second_connection_waits_on_busy_timeout(tmp_path: Path) -> None:
    db = tmp_path / "lock.db"
    url = f"sqlite:///{db}"
    first = await open_connection(url)
    second = await open_connection(url)
    await first.execute("BEGIN IMMEDIATE")

    async def begin_second() -> None:
        await second.execute("BEGIN IMMEDIATE")
        await second.commit()

    waiter = asyncio.create_task(begin_second())
    await asyncio.sleep(0.15)
    assert not waiter.done()
    await first.commit()
    await asyncio.wait_for(waiter, timeout=2)
    await first.close()
    await second.close()
