"""Storage parity across sqlite, Postgres, and Firestore (T1-STORE-01 … 16).

Sqlite always runs. Postgres and Firestore skip unless
``MONKEYBOT_TEST_POSTGRES_URL`` / ``FIRESTORE_EMULATOR_HOST`` are set.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from monkeybot.core.llm.provider import Message
from monkeybot.core.llm.usage import Usage
from monkeybot.core.persistence.branches import branch_thread_id
from monkeybot.core.persistence.durable_runs import SubagentEnvelope
from monkeybot.core.persistence.goal_ledger import (
    Channel,
    GoalEntry,
    Intent,
    Provenance,
    Status,
)
from monkeybot.core.persistence.scheduled_loops import (
    KIND_GOAL,
    OpenGoalExistsError,
    ScheduledLoopCreate,
)
from monkeybot.core.persistence.thread_summary import CONTEXT_SUMMARY_PREFIX
from monkeybot.core.persistence.usage_buckets import utc_bucket_key
from monkeybot.core.runtime.history_rewrite import (
    branch_op,
    fork_session,
    load_active_history,
)
from monkeybot.core.runtime.request_history import RequestHistory


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _goal(thread_id: str, entry_id: str) -> GoalEntry:
    return GoalEntry(
        entry_id=entry_id,
        thread_id=thread_id,
        seq=0,
        verbatim="ship the parity suite",
        provenance=Provenance.HUMAN,
        channel=Channel.MESSAGE,
        intent=Intent.NEW_GOAL,
        status=Status.ACTIVE,
        relates_to=None,
        constraints=(),
        done_when=(),
        created_at_ms=1,
    )


def _outbox(
    *,
    thread_id: str,
    message_id: str,
    agent_id: str,
    palace_id: str,
    wing: str | None = "main",
) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "thread_id": thread_id,
        "turn_id": "turn-1",
        "message_id": message_id,
        "role": "user",
        "content": message_id,
        "workspace_id": None,
        "wing": wing,
        "room": "conversation",
        "palace_id": palace_id,
    }


async def _clear_stored_row_id(backend: Any, thread_id: str, row_id: str) -> None:
    """Blank one stored row id so ``load`` reports ``legacy:<key>``."""
    conn = getattr(backend, "_conn", None)
    if conn is not None:
        await conn.execute(
            "UPDATE conversation_history SET row_id = NULL WHERE thread_id = ? AND row_id = ?",
            (thread_id, row_id),
        )
        await conn.commit()
        return
    pool = getattr(backend, "_pool", None)
    if pool is not None:
        await pool.execute(
            "UPDATE conversation_history SET row_id = NULL WHERE thread_id = $1 AND row_id = $2",
            thread_id,
            row_id,
        )
        return
    history = backend.history()
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = history._client.collection(history._collection).where(
        filter=FieldFilter("row_id", "==", row_id)
    )
    async for doc in query.stream():
        await doc.reference.update({"row_id": ""})


async def _set_outbox_created_at(backend: Any, row_id: str, created_at: str) -> None:
    conn = getattr(backend, "_conn", None)
    if conn is not None:
        await conn.execute(
            "UPDATE memory_outbox SET created_at = ? WHERE id = ?",
            (created_at, row_id),
        )
        await conn.commit()
        return
    pool = getattr(backend, "_pool", None)
    if pool is not None:
        await pool.execute(
            "UPDATE memory_outbox SET created_at = $1 WHERE id = $2",
            created_at,
            row_id,
        )
        return
    from monkeybot.core.persistence.firestore import _memory_outbox_collection

    client = backend._client
    await (
        _memory_outbox_collection(client, backend._config.prefix)
        .document(row_id)
        .update({"created_at": created_at})
    )


@pytest.mark.case("T1-STORE-01")
async def test_history_roundtrip_scope_idempotent(open_backend: Any) -> None:
    scope_a = await open_backend(agent_scope="scope-a")
    scope_b = await open_backend(agent_scope="scope-b")
    thread = _id("thread")
    message = Message.text("user", "hello")
    await scope_a.history().append(thread, message, message_id="same-message")
    await scope_a.history().append(thread, message, message_id="same-message")
    await scope_b.history().append(thread, message, message_id="same-message")

    loaded = await scope_a.history().load(thread)
    assert len(loaded) == 1
    assert loaded[0].content[0].text == "hello"
    assert await scope_b.history().list_threads() == []
    assert await scope_b.history().load(thread) == []


@pytest.mark.case("T1-STORE-02")
async def test_reset_preserves_order_and_row_ids(open_backend: Any) -> None:
    backend = await open_backend(agent_scope="scope-a")
    history = backend.history()
    thread = _id("thread")
    rows = [Message.text("user" if i % 2 == 0 else "assistant", f"m{i}") for i in range(12)]
    await history.reset(thread, rows)
    loaded = await history.load(thread)
    assert [block.text for message in loaded for block in message.content] == [
        f"m{i}" for i in range(12)
    ]
    copy = _id("copy")
    await history.reset(copy, loaded[:5])
    copied = await history.load(copy)
    assert [message.row_id for message in copied] == [message.row_id for message in loaded[:5]]


@pytest.mark.case("T1-STORE-03")
async def test_delete_rows_legacy_and_stored(open_backend: Any) -> None:
    backend = await open_backend(agent_scope="scope-a")
    history = backend.history()
    thread = _id("thread")
    await history.append(thread, Message.text("user", "legacy-row"))
    await history.append(thread, Message.text("assistant", "stored-row"))
    before = await history.load(thread)
    await _clear_stored_row_id(backend, thread, before[0].row_id or "")
    loaded = await history.load(thread)
    assert loaded[0].row_id is not None and loaded[0].row_id.startswith("legacy:")
    assert loaded[1].row_id == before[1].row_id
    assert not (loaded[1].row_id or "").startswith("legacy:")

    deleted = await history.delete_rows(thread, [loaded[0].row_id, loaded[1].row_id or ""])
    assert deleted == 2
    assert await history.load(thread) == []


@pytest.mark.case("T1-STORE-04")
async def test_hidden_threads(open_backend: Any) -> None:
    backend = await open_backend(agent_scope="scope-a")
    history = backend.history()
    session = _id("chat")
    hidden_subagent = f"subagent:{session}:child"
    hidden_branch = branch_thread_id(session, "b1")
    await history.append(session, Message.text("user", "visible"))
    await history.append(hidden_subagent, Message.text("user", "subagent"))
    await history.append(hidden_branch, Message.text("user", "branch"))

    listed = [item.thread_id for item in await history.list_threads()]
    assert session in listed
    assert hidden_subagent not in listed
    assert hidden_branch not in listed


@pytest.mark.case("T1-STORE-05")
async def test_append_with_outbox_atomic(open_backend: Any) -> None:
    backend = await open_backend(agent_scope="scope-a")
    history = backend.history()
    thread = _id("thread")
    message_id = _id("msg")
    with pytest.raises(Exception, match="(?i)constraint|not null|null"):
        await history.append_with_outbox(
            thread,
            Message.text("user", "rolled-back"),
            turn_id="turn-1",
            message_id=message_id,
            outbox=_outbox(
                thread_id=thread,
                message_id=message_id,
                agent_id="agent-a",
                palace_id="palace-a",
                wing=None,
            ),
        )
    assert await history.load(thread) == []


@pytest.mark.case("T1-STORE-06")
async def test_branches_one_active(open_backend: Any) -> None:
    backend = await open_backend(agent_scope="scope-a")
    store = backend.branches()
    session = _id("session")
    from monkeybot.core.persistence.branches import BranchRecord

    def record(branch_id: str) -> BranchRecord:
        return BranchRecord(
            branch_id=branch_id,
            session_id=session,
            thread_id=branch_thread_id(session, branch_id),
            parent_branch_id="root",
            fork_row_id="row-1",
            op="edit",
            created_at=10,
            last_active_at=10,
            is_active=False,
        )

    results = await asyncio.gather(
        *(store.create(record(f"b{i}")) for i in range(4)),
        return_exceptions=True,
    )
    created = [item for item in results if isinstance(item, BranchRecord)]
    assert created
    rows = await store.list(session)
    assert sum(row.is_active for row in rows) == 1


@pytest.mark.case("T1-STORE-07")
async def test_branch_ops_parity(open_backend: Any) -> None:
    backend = await open_backend(agent_scope="scope-a")
    history = backend.history()
    branches = backend.branches()
    session = _id("session")
    for text, role in (("u0", "user"), ("a1", "assistant"), ("u2", "user"), ("a3", "assistant")):
        await history.append(session, Message.text(role, text))
    loaded = await history.load(session)

    edited = await branch_op(
        history=history,
        branches=branches,
        session_id=session,
        op="edit",
        anchor_row_id=loaded[2].row_id or "",
    )
    edit_rows = await history.load(edited.thread_id)
    assert [row.row_id for row in edit_rows] == [loaded[0].row_id, loaded[1].row_id]
    assert (await branches.get_active(session)).branch_id == edited.branch_id

    rewound = await branch_op(
        history=history,
        branches=branches,
        session_id=session,
        op="rewind",
        anchor_row_id=edit_rows[0].row_id or "",
    )
    rewind_rows = await history.load(rewound.thread_id)
    assert [row.row_id for row in rewind_rows] == [loaded[0].row_id]

    forked = await fork_session(
        history=history,
        branches=branches,
        attachments=None,
        session_id=session,
        anchor_row_id=rewind_rows[0].row_id or "",
    )
    fork_rows = await history.load(forked.session_id)
    assert [row.row_id for row in fork_rows] == [loaded[0].row_id]

    compact_session = _id("compact")
    for text, role in (("u0", "user"), ("a1", "assistant"), ("u2", "user"), ("a3", "assistant")):
        await history.append(compact_session, Message.text(role, text))
    compact_loaded = await history.load(compact_session)
    compacted = await branch_op(
        history=history,
        branches=branches,
        session_id=compact_session,
        op="edit",
        anchor_row_id=compact_loaded[2].row_id or "",
    )
    fork_row_id = (await branches.get_active(compact_session)).fork_row_id
    assert fork_row_id == compact_loaded[1].row_id
    summary = Message.text("assistant", f"{CONTEXT_SUMMARY_PREFIX} compacted")
    await history.reset(compacted.thread_id, [summary])
    assert (await branches.get_active(compact_session)).fork_row_id == fork_row_id
    active = await load_active_history(history, branches, compact_session)
    summary_id = active.messages[0].row_id
    assert any(point["anchor"]["row_id"] == summary_id for point in active.branch_points)


@pytest.mark.case("T1-STORE-08")
async def test_usage_breakdown_buckets(open_backend: Any) -> None:
    backend = await open_backend(agent_scope="scope-a")
    before = int(time.time() * 1000)
    await backend.usage().record(
        _id("usage"),
        "parity-model",
        Usage(input_tokens=3, output_tokens=4, cost_usd=0.1),
    )
    after = int(time.time() * 1000)
    for bucket in ("hour", "day", "week"):
        report = await backend.usage().breakdown(before - 1000, bucket=bucket)
        assert report.by_bucket_model
        possible = {utc_bucket_key(before, bucket), utc_bucket_key(after, bucket)}
        assert {point.bucket for point in report.by_bucket_model} <= possible


@pytest.mark.case("T1-STORE-09")
async def test_run_lease_owner_renew_and_failed_payload(open_backend: Any, tmp_path: Path) -> None:
    backend = await open_backend(agent_scope="scope-a")
    store = backend.runs()
    run_id = _id("run")
    envelope = SubagentEnvelope(
        task="parity",
        context="ctx",
        memory_storage_uri="local:///mem",
        parent_run_id="parent",
    )
    await store.record_started(
        run_id, "parent", "worker.py", envelope, tmp_path / "scratch", owner_id="host:1:live"
    )
    assert await store.renew_claim(run_id, "host:1:live") is True
    assert await store.renew_claim(run_id, "host:1:other") is False
    stale = await store.list_stale_claims(1, now_ms=int(time.time() * 1000) + 120_000)
    assert run_id in {row.run_id for row in stale}
    payload = '{"ok": false, "exit_reason": "error"}'
    assert await store.record_failed(
        run_id, "orphaned", worker_id="host:1:live", result_json=payload
    )
    row = await store.get_run(run_id)
    assert row is not None
    assert row.status == "failed"
    assert row.result_json == payload


@pytest.mark.case("T1-STORE-10")
async def test_loop_claim_defer_complete(open_backend: Any) -> None:
    backend = await open_backend(agent_scope="scope-a")
    loops = backend.scheduled_loops()
    created = await loops.create(
        ScheduledLoopCreate(
            prompt="ping",
            interval_ms=1000,
            session_id=_id("loop-session"),
            loop_id=_id("loop"),
            max_ticks=3,
        )
    )
    claimed = await loops.claim_tick(created.loop_id, "worker-a", now_ms=created.next_tick_at_ms)
    assert claimed is not None
    assert (
        await loops.claim_tick(created.loop_id, "worker-b", now_ms=created.next_tick_at_ms) is None
    )
    assert await loops.defer_tick(
        created.loop_id,
        worker_id="worker-a",
        reason="busy",
        now_ms=created.next_tick_at_ms,
    )
    again_at = created.next_tick_at_ms + created.interval_ms
    reclaimed = await loops.claim_tick(created.loop_id, "worker-a", now_ms=again_at)
    assert reclaimed is not None
    done = await loops.complete_tick(created.loop_id, worker_id="worker-a", now_ms=again_at)
    assert done is not None and done.tick_index == 1


@pytest.mark.case("T1-STORE-11")
async def test_turn_lock_exclusive_and_stale(open_backend: Any) -> None:
    backend = await open_backend(agent_scope="scope-a")
    locks = backend.session_turns()
    session = _id("turn")
    assert await locks.try_acquire(session, "request-1")
    assert await locks.is_busy(session)
    assert await locks.try_acquire(session, "request-2") is False
    later = int(time.time() * 1000) + 10**9
    assert await locks.release_stale_claims(1, now_ms=later) == 1
    assert await locks.is_busy(session) is False
    assert await locks.try_acquire(session, "request-3")


@pytest.mark.case("T1-STORE-12")
async def test_outbox_claim_scoped(open_backend: Any) -> None:
    backend = await open_backend(agent_scope="scope-a")
    history = backend.history()
    thread = _id("thread")
    mine = _id("mine")
    other = _id("other")
    await history.append_with_outbox(
        thread,
        Message.text("user", "mine"),
        turn_id="turn-1",
        message_id=mine,
        outbox=_outbox(thread_id=thread, message_id=mine, agent_id="agent-a", palace_id="palace-a"),
    )
    await history.append_with_outbox(
        thread,
        Message.text("user", "other"),
        turn_id="turn-1",
        message_id=other,
        outbox=_outbox(
            thread_id=thread, message_id=other, agent_id="agent-b", palace_id="palace-b"
        ),
    )
    outbox = backend.outbox()
    claimed = await outbox.claim_batch(
        agent_id="agent-a", palace_id="palace-a", lease_owner="worker"
    )
    assert [row.agent_id for row in claimed] == ["agent-a"]
    assert [row.palace_id for row in claimed] == ["palace-a"]
    other_claimed = await outbox.claim_batch(
        agent_id="agent-b", palace_id="palace-b", lease_owner="worker"
    )
    assert [row.agent_id for row in other_claimed] == ["agent-b"]
    ids = [row.id for row in (*claimed, *other_claimed)]
    assert await outbox.mark_committed(ids) == 2
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
    await _set_outbox_created_at(backend, claimed[0].id, old)
    assert await outbox.gc_committed(days=7) == 1


@pytest.mark.case("T1-STORE-13")
async def test_goal_ledger_presence(open_backend: Any) -> None:
    backend = await open_backend(agent_scope="scope-a")
    if open_backend.kind != "sqlite":
        assert backend.goal_ledger() is None
        return
    store = backend.goal_ledger()
    thread = _id("goals")
    entry_id = _id("goal")
    first = await store.commit_classified(_goal(thread, entry_id))
    assert first.status == Status.ACTIVE
    with pytest.raises(sqlite3.IntegrityError):
        await store.commit_classified(
            _goal(thread, entry_id),
            status_updates=[(entry_id, Status.SATISFIED)],
        )
    rows = await store.list_entries(thread)
    assert len(rows) == 1
    assert rows[0].status == Status.ACTIVE
    assert rows[0].verbatim == first.verbatim


@pytest.mark.case("T1-STORE-14")
async def test_known_unscoped_tables(open_backend: Any, tmp_path: Path) -> None:
    scope_a = await open_backend(agent_scope="scope-a")
    scope_b = await open_backend(agent_scope="scope-b")
    thread = _id("usage")
    await scope_a.usage().record(thread, "parity-model", Usage(input_tokens=5, output_tokens=1))
    assert (await scope_b.usage().summary(thread)).turns == 1

    run_id = _id("run")
    await scope_a.runs().record_started(
        run_id,
        None,
        "worker.py",
        SubagentEnvelope(
            task="parity",
            context="ctx",
            memory_storage_uri="local:///mem",
            parent_run_id="parent",
        ),
        tmp_path / "scratch",
    )
    assert (await scope_b.runs().get_run(run_id)) is not None

    loop_id = _id("loop")
    await scope_a.scheduled_loops().create(
        ScheduledLoopCreate(
            prompt="ping",
            interval_ms=1000,
            session_id=_id("loop-session"),
            loop_id=loop_id,
            max_ticks=2,
        )
    )
    assert await scope_b.scheduled_loops().get(loop_id) is not None

    session = _id("lock")
    assert await scope_a.session_turns().try_acquire(session, "request-1")
    assert await scope_b.session_turns().is_busy(session)


@pytest.mark.case("T1-STORE-15")
async def test_waiting_goal_is_one_open_goal(open_backend: Any) -> None:
    backend = await open_backend(agent_scope="scope-a")
    loops = backend.scheduled_loops()
    session = _id("goal-session")
    first = await loops.create(
        ScheduledLoopCreate(
            prompt="First",
            interval_ms=60_000,
            session_id=session,
            loop_id=_id("goal"),
            kind=KIND_GOAL,
            objective="First",
        )
    )
    assert await loops.set_waiting(first.loop_id, reason="review", wake_at_ms=None)
    with pytest.raises(OpenGoalExistsError):
        await loops.create(
            ScheduledLoopCreate(
                prompt="Second",
                interval_ms=60_000,
                session_id=session,
                loop_id=_id("goal-2"),
                kind=KIND_GOAL,
                objective="Second",
            )
        )


def _row_sig(messages: list[Message]) -> list[tuple[str, str | None, list[dict[str, object]]]]:
    return [
        (message.role, message.row_id, [block.to_dict() for block in message.content])
        for message in messages
    ]


class _CountLoads:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.loads = 0

    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        self.loads += 1
        return await self._inner.load(thread_id, limit)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.mark.case("T1-STORE-16")
async def test_request_history_snapshot_matches_load(open_backend: Any) -> None:
    backend = await open_backend(agent_scope="scope-a")
    history = backend.history()
    counted = _CountLoads(history)
    thread = _id("thread")
    message_id = _id("msg")
    wrapped = RequestHistory.wrap(counted, thread_id=thread)
    await wrapped.append(thread, Message.text("user", "one"))
    await wrapped.append_with_outbox(
        thread,
        Message.text("assistant", "two"),
        turn_id="turn-1",
        message_id=message_id,
        outbox=_outbox(
            thread_id=thread,
            message_id=message_id,
            agent_id="agent-a",
            palace_id="palace-a",
        ),
    )

    snap = await wrapped.load(thread)
    assert counted.loads == 1
    assert _row_sig(snap) == _row_sig(await history.load(thread))
    assert _row_sig(await wrapped.load(thread)) == _row_sig(snap)
    assert counted.loads == 1

    await wrapped.reset(
        thread,
        [Message.text("user", "kept"), Message.text("assistant", "tail")],
    )
    assert counted.loads == 1
    reset = await wrapped.load(thread)
    assert _row_sig(reset) == _row_sig(await history.load(thread))
    assert [block.text for message in reset for block in message.content] == ["kept", "tail"]
    assert counted.loads == 1


async def _history_meta(backend: Any, thread_id: str) -> list[dict[str, Any]]:
    """``created_at``, ``turn_id``, and ``message_id`` in load order."""
    scope = backend._agent_scope
    conn = getattr(backend, "_conn", None)
    if conn is not None:
        cursor = await conn.execute(
            """
            SELECT created_at, turn_id, message_id
            FROM conversation_history
            WHERE thread_id = ? AND agent_scope = ?
            ORDER BY created_at ASC, id ASC
            """,
            (thread_id, scope),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            {"created_at": int(row[0]), "turn_id": row[1], "message_id": row[2]} for row in rows
        ]
    pool = getattr(backend, "_pool", None)
    if pool is not None:
        fetched = await pool.fetch(
            """
            SELECT created_at, turn_id, message_id
            FROM conversation_history
            WHERE thread_id = $1 AND agent_scope = $2
            ORDER BY created_at ASC, id ASC
            """,
            thread_id,
            scope,
        )
        return [
            {
                "created_at": int(row["created_at"]),
                "turn_id": row["turn_id"],
                "message_id": row["message_id"],
            }
            for row in fetched
        ]
    history = backend.history()
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = (
        history._client.collection(history._collection)
        .where(filter=FieldFilter("thread_id", "==", thread_id))
        .where(filter=FieldFilter("agent_scope", "==", history._agent_scope))
        .order_by("created_at")
    )
    out: list[dict[str, Any]] = []
    async for doc in query.stream():
        data = doc.to_dict() or {}
        out.append(
            {
                "created_at": int(data.get("created_at") or 0),
                "turn_id": data.get("turn_id"),
                "message_id": data.get("message_id"),
            }
        )
    return out


async def _stamp_history(
    backend: Any,
    thread_id: str,
    specs: list[tuple[int, str, str]],
) -> None:
    """Set ``created_at``, ``turn_id``, and ``message_id`` on the thread's rows, in order."""
    scope = backend._agent_scope
    conn = getattr(backend, "_conn", None)
    if conn is not None:
        cursor = await conn.execute(
            """
            SELECT id FROM conversation_history
            WHERE thread_id = ? AND agent_scope = ?
            ORDER BY created_at ASC, id ASC
            """,
            (thread_id, scope),
        )
        ids = [row[0] for row in await cursor.fetchall()]
        await cursor.close()
        assert len(ids) == len(specs)
        for db_id, (created_at, turn_id, message_id) in zip(ids, specs, strict=True):
            await conn.execute(
                """
                UPDATE conversation_history
                SET created_at = ?, turn_id = ?, message_id = ?
                WHERE id = ?
                """,
                (created_at, turn_id, message_id, db_id),
            )
        await conn.commit()
        return
    pool = getattr(backend, "_pool", None)
    if pool is not None:
        fetched = await pool.fetch(
            """
            SELECT id FROM conversation_history
            WHERE thread_id = $1 AND agent_scope = $2
            ORDER BY created_at ASC, id ASC
            """,
            thread_id,
            scope,
        )
        assert len(fetched) == len(specs)
        for row, (created_at, turn_id, message_id) in zip(fetched, specs, strict=True):
            await pool.execute(
                """
                UPDATE conversation_history
                SET created_at = $1, turn_id = $2, message_id = $3
                WHERE id = $4
                """,
                created_at,
                turn_id,
                message_id,
                row["id"],
            )
        return
    history = backend.history()
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = (
        history._client.collection(history._collection)
        .where(filter=FieldFilter("thread_id", "==", thread_id))
        .where(filter=FieldFilter("agent_scope", "==", history._agent_scope))
        .order_by("created_at")
    )
    docs = [doc async for doc in query.stream()]
    assert len(docs) == len(specs)
    for doc, (created_at, turn_id, message_id) in zip(docs, specs, strict=True):
        await doc.reference.update(
            {"created_at": created_at, "turn_id": turn_id, "message_id": message_id}
        )
    await (
        history._client.collection(history._threads_collection)
        .document(history._summary_doc_id(thread_id))
        .set({"last_message_at": specs[-1][0]}, merge=True)
    )


@pytest.mark.case("T1-STORE-17")
async def test_reset_keeps_row_metadata(open_backend: Any) -> None:
    backend = await open_backend(agent_scope="scope-a")
    history = backend.history()
    thread = _id("thread")
    roles = ("user", "assistant", "user", "assistant")
    texts = ("head", "middle", "tail-a", "tail-b")
    for role, text in zip(roles, texts, strict=True):
        await history.append(thread, Message.text(role, text))
    message_ids = [_id("msg") for _ in texts]
    specs = [
        (1_000, "turn-a", message_ids[0]),
        (2_000, "turn-a", message_ids[1]),
        (3_000, "turn-b", message_ids[2]),
        (4_000, "turn-b", message_ids[3]),
    ]
    await _stamp_history(backend, thread, specs)
    listed = await history.list_threads()
    before = next(item for item in listed if item.thread_id == thread)
    assert before.last_message_at == 4_000

    loaded = await history.load(thread)
    await history.reset(
        thread,
        [
            loaded[0],
            Message.text("assistant", "summary"),
            loaded[2],
            loaded[3],
        ],
    )
    meta = await _history_meta(backend, thread)
    kept = await history.load(thread)
    assert [block.text for message in kept for block in message.content] == [
        "head",
        "summary",
        "tail-a",
        "tail-b",
    ]
    assert [row["created_at"] for row in meta] == [1_000, meta[1]["created_at"], 3_000, 4_000]
    assert 1_000 <= meta[1]["created_at"] <= 3_000
    assert meta[0]["turn_id"] == "turn-a" and meta[0]["message_id"] == message_ids[0]
    assert meta[1]["turn_id"] is None and meta[1]["message_id"] is None
    assert meta[2]["turn_id"] == "turn-b" and meta[2]["message_id"] == message_ids[2]
    assert meta[3]["turn_id"] == "turn-b" and meta[3]["message_id"] == message_ids[3]
    assert [message.row_id for message in kept[:1] + kept[2:]] == [
        loaded[0].row_id,
        loaded[2].row_id,
        loaded[3].row_id,
    ]
    after = next(item for item in await history.list_threads() if item.thread_id == thread)
    assert after.last_message_at == before.last_message_at

    await history.append(thread, Message.text("user", "head"), message_id=message_ids[0])
    assert len(await history.load(thread)) == 4

    copy = _id("copy")
    await history.reset(copy, await history.load(thread))
    copied = await history.load(copy)
    assert [block.text for message in copied for block in message.content] == [
        "head",
        "summary",
        "tail-a",
        "tail-b",
    ]
    assert all(row["message_id"] is None for row in await _history_meta(backend, copy))
    await history.append(thread, Message.text("user", "head"), message_id=message_ids[0])
    assert len(await history.load(thread)) == 4
