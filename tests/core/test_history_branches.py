"""Branch store, branch ops, and the row-id navigator."""

from __future__ import annotations

import json
import random
from dataclasses import replace
from typing import Any

import pytest
import pytest_asyncio

from monkeybot.core.attachments.store import FilesystemAttachmentStore
from monkeybot.core.attachments.text import render_attachment_descriptor_text
from monkeybot.core.llm.provider import Message
from monkeybot.core.persistence.branches import (
    ROOT_BRANCH_ID,
    BranchRecord,
    SQLiteBranchStore,
    branch_thread_id,
)
from monkeybot.core.persistence.history import SQLiteHistoryStore
from monkeybot.core.persistence.sqlite import TaskReentrantLock, apply_schema, open_connection
from monkeybot.core.persistence.thread_summary import (
    CONTEXT_SUMMARY_PREFIX,
    is_user_text_row,
)
from monkeybot.core.runtime.history_rewrite import (
    HistoryRewriteError,
    activate_branch,
    assert_tool_pairs,
    branch_op,
    divergence_points,
    fork_session,
    load_active_history,
    overlay_active_previews,
    purge_session_branches,
    resolve_active_thread_id,
    truncate_active,
)
from monkeybot.core.types.content_blocks import AttachmentRef, Text, ToolRequest, ToolResponse

SESSION = "s1"


def _text(role: str, text: str) -> Message:
    return Message(role=role, content=[Text(text=text)])  # type: ignore[arg-type]


class _Env:
    def __init__(self, history: SQLiteHistoryStore, branches: SQLiteBranchStore) -> None:
        self.history = history
        self.branches = branches

    def branches_factory(self) -> SQLiteBranchStore:
        return self.branches

    async def thread(self) -> str:
        return await resolve_active_thread_id(_Backend(self.branches), SESSION)

    async def turn(self, user: str, reply: str) -> None:
        thread = await self.thread()
        await self.history.append(thread, _text("user", user))
        await self.history.append(thread, _text("assistant", reply))

    async def rows(self) -> list[Message]:
        return list(await self.history.load(await self.thread()))

    async def texts(self) -> list[str]:
        return [m.content[0].text for m in await self.rows()]  # type: ignore[union-attr]

    async def op(self, op: str, row_id: str) -> Any:
        return await branch_op(
            history=self.history,
            branches=self.branches,
            session_id=SESSION,
            op=op,  # type: ignore[arg-type]
            anchor_row_id=row_id,
        )

    async def points(self) -> list[dict[str, Any]]:
        return (await load_active_history(self.history, self.branches, SESSION)).branch_points


class _Backend:
    def __init__(self, branches: SQLiteBranchStore) -> None:
        self._branches = branches

    def branches(self) -> SQLiteBranchStore:
        return self._branches


@pytest_asyncio.fixture
async def env():
    conn = await open_connection("sqlite:///:memory:")
    await apply_schema(conn)
    lock = TaskReentrantLock()
    yield _Env(
        SQLiteHistoryStore(conn, agent_scope="a", lock=lock),
        SQLiteBranchStore(conn, agent_scope="a", lock=lock),
    )
    await conn.close()


def _record(
    branch_id: str, *, parent: str | None = ROOT_BRANCH_ID, fork: str | None = "r"
) -> BranchRecord:
    return BranchRecord(
        branch_id=branch_id,
        session_id=SESSION,
        thread_id=branch_thread_id(SESSION, branch_id),
        parent_branch_id=parent,
        fork_row_id=fork,
        op="edit",
        created_at=1,
        last_active_at=1,
        is_active=True,
    )


# --- store -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_adds_inactive_root_and_keeps_one_active(env) -> None:
    store = env.branches
    assert await store.list(SESSION) == []
    await store.create(_record("b1"))
    await store.create(_record("b2"))
    rows = await store.list(SESSION)
    assert {r.branch_id for r in rows} == {ROOT_BRANCH_ID, "b1", "b2"}
    root = next(r for r in rows if r.branch_id == ROOT_BRANCH_ID)
    assert root.thread_id == SESSION and root.parent_branch_id is None
    assert [r.branch_id for r in rows if r.is_active] == ["b2"]
    assert (await store.get_active(SESSION)).branch_id == "b2"  # type: ignore[union-attr]
    switched = await store.set_active(SESSION, ROOT_BRANCH_ID)
    assert switched is not None and switched.is_active
    assert (await store.get_active(SESSION)).branch_id == ROOT_BRANCH_ID  # type: ignore[union-attr]
    assert await store.set_active(SESSION, "missing") is None
    assert (await store.get_active(SESSION)).branch_id == ROOT_BRANCH_ID  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_store_is_scoped_by_agent(env) -> None:
    await env.branches.create(_record("b1"))
    other = SQLiteBranchStore(env.branches._conn, agent_scope="other")
    assert await other.list(SESSION) == []
    assert await other.get_active(SESSION) is None
    await other.create(_record("b1"))
    assert len(await env.branches.list(SESSION)) == 2
    assert [r.branch_id for r in await other.delete_session(SESSION)] != []
    assert len(await env.branches.list(SESSION)) == 2


@pytest.mark.asyncio
async def test_active_non_root_and_delete(env) -> None:
    await env.branches.create(_record("b1"))
    other_session = replace(_record("x"), session_id="s2", thread_id=branch_thread_id("s2", "x"))
    await env.branches.create(other_session)
    await env.branches.set_active("s2", ROOT_BRANCH_ID)
    found = await env.branches.active_non_root([SESSION, "s2", "s3"])
    assert set(found) == {SESSION}
    deleted = await env.branches.delete_session(SESSION)
    assert {r.branch_id for r in deleted} == {ROOT_BRANCH_ID, "b1"}
    assert await env.branches.list(SESSION) == []


# --- ops ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_copies_prefix_and_switches(env) -> None:
    await env.turn("u1", "a1")
    await env.turn("u2", "a2")
    root_rows = await env.rows()
    result = await env.op("edit", root_rows[2].row_id)
    assert result.replay_content is None
    assert await env.thread() == branch_thread_id(SESSION, result.branch_id)
    copied = await env.rows()
    assert [m.row_id for m in copied] == [m.row_id for m in root_rows[:2]]
    record = await env.branches.get_active(SESSION)
    assert record.fork_row_id == root_rows[1].row_id  # type: ignore[union-attr]
    assert record.parent_branch_id == ROOT_BRANCH_ID  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_edit_rejects_non_user_rows_and_unknown_anchor(env) -> None:
    await env.turn("u1", "a1")
    rows = await env.rows()
    with pytest.raises(HistoryRewriteError) as exc:
        await env.op("edit", rows[1].row_id)
    assert exc.value.code == "TURN_BOUNDARY"
    with pytest.raises(HistoryRewriteError) as exc:
        await env.op("edit", "nope")
    assert exc.value.code == "ANCHOR_MISMATCH"
    assert await env.branches.list(SESSION) == []


@pytest.mark.asyncio
async def test_regenerate_returns_owning_user_content(env) -> None:
    await env.turn("u1", "a1")
    rows = await env.rows()
    result = await env.op("regenerate", rows[1].row_id)
    assert [b.text for b in result.replay_content] == ["u1"]  # type: ignore[union-attr]
    assert await env.rows() == []
    assert (await env.branches.get_active(SESSION)).fork_row_id is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_rewind_keeps_turn_and_requires_a_drop(env) -> None:
    await env.turn("u1", "a1")
    await env.turn("u2", "a2")
    rows = await env.rows()
    with pytest.raises(HistoryRewriteError) as exc:
        await env.op("rewind", rows[3].row_id)
    assert exc.value.code == "NOTHING_TO_REWIND"
    await env.op("rewind", rows[1].row_id)
    assert await env.texts() == ["u1", "a1"]


@pytest.mark.asyncio
async def test_ops_refuse_rows_at_or_before_the_summary(env) -> None:
    await env.turn("u1", "a1")
    rows = await env.rows()
    summary = _text("assistant", f"{CONTEXT_SUMMARY_PREFIX}\nearlier")
    # Compaction mid-turn: the continuation after the summary has no user row of its own.
    await env.history.reset(await env.thread(), [rows[0], summary, _text("assistant", "continued")])
    rows = await env.rows()
    for op, row in [("regenerate", rows[2]), ("edit", rows[0]), ("rewind", rows[0])]:
        with pytest.raises(HistoryRewriteError) as exc:
            await env.op(op, row.row_id)
        assert exc.value.code == "SUMMARIZED", op
    with pytest.raises(HistoryRewriteError) as exc:
        await _truncate(env, rows[0].row_id)
    assert exc.value.code == "SUMMARIZED"
    assert await env.branches.list(SESSION) == []


@pytest.mark.asyncio
async def test_ops_after_the_summary_and_forks_are_allowed(env) -> None:
    await env.turn("u1", "a1")
    await env.turn("u2", "a2")
    rows = await env.rows()
    summary = _text("assistant", f"{CONTEXT_SUMMARY_PREFIX}\nearlier")
    await env.history.reset(
        await env.thread(), [*rows, summary, _text("user", "u3"), _text("assistant", "a3")]
    )
    rows = await env.rows()
    folded = ["u1", "a1", "u2", "a2", f"{CONTEXT_SUMMARY_PREFIX}\nearlier"]
    with pytest.raises(HistoryRewriteError) as exc:
        await env.op("restore", rows[2].row_id)
    assert exc.value.code == "SUMMARIZED"
    forked = await fork_session(
        history=env.history,
        branches=env.branches,
        attachments=None,
        session_id=SESSION,
        anchor_row_id=rows[0].row_id,
    )
    assert [m.row_id for m in await env.history.load(forked.session_id)] == [rows[0].row_id]
    # The summary row's turn started before it, so it is read-only like the wire says.
    for row in (rows[4], rows[3]):
        with pytest.raises(HistoryRewriteError) as exc:
            await env.op("rewind", row.row_id)
        assert exc.value.code == "SUMMARIZED"
        with pytest.raises(HistoryRewriteError) as exc:
            await _truncate(env, row.row_id)
        assert exc.value.code == "SUMMARIZED"
    await env.op("rewind", rows[5].row_id)
    assert await env.texts() == [*folded, "u3"]
    await activate_branch(branches=env.branches, session_id=SESSION, branch_id=ROOT_BRANCH_ID)
    result = await env.op("restore", rows[5].row_id)
    assert result.replay_content is None
    assert await env.texts() == folded


@pytest.mark.asyncio
async def test_restore_rejects_non_user_and_first_rows(env) -> None:
    await env.turn("u1", "a1")
    await env.turn("u2", "a2")
    rows = await env.rows()
    with pytest.raises(HistoryRewriteError) as exc:
        await env.op("restore", rows[1].row_id)
    assert exc.value.code == "TURN_BOUNDARY"
    with pytest.raises(HistoryRewriteError) as exc:
        await env.op("restore", rows[0].row_id)
    assert exc.value.code == "NOTHING_TO_KEEP"
    result = await env.op("restore", rows[2].row_id)
    assert result.replay_content is None
    assert await env.texts() == ["u1", "a1"]


def test_cut_between_tool_pair_is_rejected_but_old_orphans_are_not() -> None:
    rows = [
        _text("user", "u1"),
        Message(role="assistant", content=[ToolRequest(id="c1", name="t", args={})]),
        Message(role="user", content=[ToolResponse(id="c1", tool_name="t", result=[])]),
        _text("assistant", "done"),
    ]
    with pytest.raises(HistoryRewriteError) as exc:
        assert_tool_pairs(rows, 2)
    assert exc.value.code == "TURN_BOUNDARY"
    assert_tool_pairs(rows, 3)
    orphan = [Message(role="assistant", content=[ToolRequest(id="c0", name="t", args={})]), *rows]
    assert_tool_pairs(orphan, 1)


@pytest.mark.asyncio
async def test_branch_cleanup_when_record_insert_fails(env, monkeypatch) -> None:
    await env.turn("u1", "a1")
    rows = await env.rows()

    async def boom(record: BranchRecord) -> BranchRecord:
        raise RuntimeError("db down")

    monkeypatch.setattr(env.branches, "create", boom)
    with pytest.raises(RuntimeError):
        await env.op("rewind", rows[0].row_id)
    threads = await env.history.list_threads()
    assert [t.thread_id for t in threads] == [SESSION]


@pytest.mark.asyncio
async def test_activate_unbranched_root_is_noop_and_unknown_is_404(env) -> None:
    record = await activate_branch(
        branches=env.branches, session_id=SESSION, branch_id=ROOT_BRANCH_ID
    )
    assert record.thread_id == SESSION
    assert await env.branches.list(SESSION) == []
    with pytest.raises(HistoryRewriteError) as exc:
        await activate_branch(branches=env.branches, session_id=SESSION, branch_id="nope")
    assert exc.value.status_code == 404


# --- navigator ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_point_sits_on_the_edited_message_on_both_versions(env) -> None:
    await env.turn("u1", "a1")
    await env.turn("u2", "a2")
    root_rows = await env.rows()
    result = await env.op("edit", root_rows[2].row_id)
    await env.turn("u2-edited", "a2b")
    [point] = await env.points()
    branch_rows = await env.rows()
    assert point["anchor"] == {"row_id": branch_rows[2].row_id}
    assert point["options"] == [ROOT_BRANCH_ID, result.branch_id]
    assert point["active_index"] == 1

    await activate_branch(branches=env.branches, session_id=SESSION, branch_id=ROOT_BRANCH_ID)
    [point] = await env.points()
    assert point["anchor"] == {"row_id": root_rows[2].row_id}
    assert point["active_index"] == 0


@pytest.mark.asyncio
async def test_editing_same_message_again_adds_an_option_to_one_point(env) -> None:
    await env.turn("u1", "a1")
    await env.turn("u2", "a2")
    first = await env.op("edit", (await env.rows())[2].row_id)
    await env.turn("v2", "b2")
    second = await env.op("edit", (await env.rows())[2].row_id)
    await env.turn("w2", "c2")
    [point] = await env.points()
    assert point["options"] == [ROOT_BRANCH_ID, first.branch_id, second.branch_id]
    assert point["active_index"] == 2


@pytest.mark.asyncio
async def test_rewind_point_sits_on_the_kept_row(env) -> None:
    await env.turn("u1", "a1")
    await env.turn("u2", "a2")
    rows = await env.rows()
    await env.op("rewind", rows[1].row_id)
    [point] = await env.points()
    assert point["anchor"] == {"row_id": rows[1].row_id}
    assert point["active_index"] == 1


@pytest.mark.asyncio
async def test_first_message_edit_point_is_row_zero(env) -> None:
    await env.turn("u1", "a1")
    await env.op("edit", (await env.rows())[0].row_id)
    await env.turn("v1", "b1")
    [point] = await env.points()
    assert point["anchor"] == {"row_id": (await env.rows())[0].row_id}


@pytest.mark.asyncio
async def test_point_survives_compaction_of_the_fork_row(env) -> None:
    for i in range(3):
        await env.turn(f"u{i}", f"a{i}")
    rows = await env.rows()
    edited = await env.op("edit", rows[4].row_id)
    await env.turn("v2", "b2")
    branch_rows = await env.rows()
    summary = _text("assistant", f"{CONTEXT_SUMMARY_PREFIX}\nearlier")
    await env.history.reset(await env.thread(), [branch_rows[0], summary, *branch_rows[4:]])
    [point] = await env.points()
    assert point["options"] == [ROOT_BRANCH_ID, edited.branch_id]
    assert point["active_index"] == 1


@pytest.mark.asyncio
async def test_unrelated_branch_shows_no_point(env) -> None:
    await env.turn("u1", "a1")
    await env.turn("u2", "a2")
    await env.turn("u3", "a3")
    rows = await env.rows()
    await env.op("edit", rows[4].row_id)
    await env.turn("v3", "b3")
    await activate_branch(branches=env.branches, session_id=SESSION, branch_id=ROOT_BRANCH_ID)
    await env.op("edit", rows[2].row_id)
    await env.turn("v2", "b2")
    # The second branch left before u3, so only its own point (at u2) shows.
    [point] = await env.points()
    assert point["anchor"] == {"row_id": (await env.rows())[2].row_id}
    assert len(point["options"]) == 2


@pytest.mark.asyncio
async def test_limit_drops_points_before_the_tail(env) -> None:
    await env.turn("u1", "a1")
    await env.turn("u2", "a2")
    await env.op("edit", (await env.rows())[0].row_id)
    await env.turn("v1", "b1")
    await env.turn("v2", "b2")
    shown = await load_active_history(env.history, env.branches, SESSION, limit=2)
    assert len(shown.messages) == 2
    assert shown.branch_points == []
    full = await load_active_history(env.history, env.branches, SESSION, limit=4)
    assert len(full.branch_points) == 1


def _reachable(records: list[BranchRecord], threads: dict[str, list[Message]]) -> set[str]:
    seen = {ROOT_BRANCH_ID}
    frontier = [ROOT_BRANCH_ID]
    while frontier:
        current = frontier.pop()
        for point in divergence_points(records, current, threads[current]):
            assert point["options"][point["active_index"]] == current
            assert len(set(point["options"])) == len(point["options"])
            for option in point["options"]:
                if option not in seen:
                    seen.add(option)
                    frontier.append(option)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(12))
async def test_random_ops_keep_every_branch_reachable(env, seed: int) -> None:
    rng = random.Random(seed)
    counter = 0

    async def turn() -> None:
        nonlocal counter
        counter += 1
        await env.turn(f"u{counter}", f"a{counter}")

    await turn()
    for _ in range(40):
        rows = await env.rows()
        choice = rng.random()
        try:
            if choice < 0.3 or not rows:
                await turn()
            elif choice < 0.5:
                users = [m for m in rows if is_user_text_row(m)]
                await env.op("edit", rng.choice(users).row_id)
                await turn()
            elif choice < 0.62:
                result = await env.op("regenerate", rng.choice(rows).row_id)
                assert result.replay_content
                thread = await env.thread()
                await env.history.append(
                    thread, Message(role="user", content=result.replay_content)
                )
                await env.history.append(thread, _text("assistant", f"regen{counter}"))
            elif choice < 0.68:
                await env.op("rewind", rng.choice(rows).row_id)
            elif choice < 0.76:
                await _truncate(env, rng.choice(rows).row_id)
            elif choice < 0.92:
                points = await env.points()
                if points:
                    point = rng.choice(points)
                    await activate_branch(
                        branches=env.branches,
                        session_id=SESSION,
                        branch_id=rng.choice(point["options"]),
                    )
            elif len(rows) > 4:
                summary = _text("assistant", f"{CONTEXT_SUMMARY_PREFIX}\nc{counter}")
                await env.history.reset(await env.thread(), [rows[0], summary, *rows[-2:]])
        except HistoryRewriteError as exc:
            assert exc.code in {
                "NOTHING_TO_REWIND",
                "NOTHING_TO_TRUNCATE",
                "TURN_BOUNDARY",
                "BRANCHES_IN_TAIL",
                "SUMMARIZED",
            }

        records = await env.branches.list(SESSION)
        if not records:
            continue
        threads = {r.branch_id: list(await env.history.load(r.thread_id)) for r in records}
        assert _reachable(records, threads) == {r.branch_id for r in records}, seed
        assert sum(r.is_active for r in records) == 1


# --- sidebar & purge -----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_threads_hides_branches_and_overlay_shows_active(env) -> None:
    await env.turn("u1", "a1")
    await env.op("edit", (await env.rows())[0].row_id)
    await env.turn("edited", "reply")
    threads = await env.history.list_threads()
    assert [t.thread_id for t in threads] == [SESSION]
    [row] = await overlay_active_previews(env.history, env.branches, threads)
    assert row.preview == "reply"
    assert row.message_count == 2


@pytest.mark.asyncio
async def test_purge_wipes_every_thread_and_branch_row(env) -> None:
    await env.turn("u1", "a1")
    result = await env.op("edit", (await env.rows())[0].row_id)
    await env.turn("edited", "reply")
    await purge_session_branches(env.history, env.branches, SESSION)
    assert await env.history.load(SESSION) == []
    assert await env.history.load(result.thread_id) == []
    assert await env.branches.list(SESSION) == []


@pytest.mark.asyncio
async def test_resolve_without_branch_support_stays_on_session() -> None:
    assert await resolve_active_thread_id(object(), SESSION) == SESSION


# --- truncate and fork -------------------------------------------------------


async def _truncate(env: _Env, row_id: str | None) -> Any:
    return await truncate_active(
        history=env.history, branches=env.branches, session_id=SESSION, anchor_row_id=row_id
    )


@pytest.mark.asyncio
async def test_truncate_keeps_anchor_turn_in_place(env) -> None:
    await env.turn("u1", "a1")
    await env.turn("u2", "a2")
    rows = await env.rows()
    result = await _truncate(env, rows[0].row_id)
    assert (result.branch_id, result.dropped) == (ROOT_BRANCH_ID, 3)
    assert await env.rows() == rows[:1]
    for anchor, code in ((rows[0].row_id, "NOTHING_TO_TRUNCATE"), ("gone", "ANCHOR_MISMATCH")):
        with pytest.raises(HistoryRewriteError) as exc:
            await _truncate(env, anchor)
        assert exc.value.code == code


@pytest.mark.asyncio
async def test_truncate_deletes_legacy_rows_by_derived_id(env) -> None:
    for role, text in (("user", "old-u"), ("assistant", "old-a")):
        await env.history._conn.execute(
            "INSERT INTO conversation_history (thread_id, role, content, created_at, agent_scope)"
            " VALUES (?, ?, ?, 1, 'a')",
            (SESSION, role, json.dumps([Text(text=text).to_dict()])),
        )
    await env.history._conn.commit()
    rows = await env.rows()
    assert all(m.row_id.startswith("legacy:") for m in rows)  # type: ignore[union-attr]
    await _truncate(env, rows[0].row_id)
    assert await env.texts() == ["old-u"]


@pytest.mark.asyncio
async def test_truncate_refuses_to_strand_a_branch(env) -> None:
    await env.turn("u1", "a1")
    await env.turn("u2", "a2")
    root_rows = await env.rows()
    branch = await env.op("edit", root_rows[2].row_id)
    await env.turn("v2", "b2")
    branch_rows = await env.rows()

    # The edit forked at a1; dropping it from either side loses the navigator.
    with pytest.raises(HistoryRewriteError) as exc:
        await _truncate(env, branch_rows[0].row_id)
    assert exc.value.code == "BRANCHES_IN_TAIL"
    await activate_branch(branches=env.branches, session_id=SESSION, branch_id=ROOT_BRANCH_ID)
    with pytest.raises(HistoryRewriteError) as exc:
        await _truncate(env, root_rows[0].row_id)
    assert exc.value.code == "BRANCHES_IN_TAIL"

    # Keeping the fork row keeps both versions reachable.
    await _truncate(env, root_rows[1].row_id)
    assert await env.texts() == ["u1", "a1"]
    [point] = await env.points()
    assert point["options"] == [ROOT_BRANCH_ID, branch.branch_id]
    await activate_branch(branches=env.branches, session_id=SESSION, branch_id=branch.branch_id)
    assert await env.texts() == ["u1", "a1", "v2", "b2"]
    assert len(await env.points()) == 1


@pytest.mark.asyncio
async def test_fork_copies_active_prefix_and_its_attachments(env, tmp_path) -> None:
    store = FilesystemAttachmentStore(tmp_path)
    kept = store.save(SESSION, data=b"\x89PNG\r\n\x1a\nkept", mime_type="image/png", filename="k")
    later = store.save(SESSION, data=b"\x89PNG\r\n\x1a\nlater", mime_type="image/png", filename="l")
    descriptor = render_attachment_descriptor_text(
        attachment_id=kept.attachment_id, filename="k", mime_type="image/png", description=""
    )
    await env.history.append(SESSION, Message(role="user", content=[Text(text=descriptor)]))
    await env.history.append(SESSION, _text("assistant", "a1"))
    await env.history.append(
        SESSION,
        Message(
            role="user",
            content=[
                Text(text="and this"),
                AttachmentRef(attachment_id=later.attachment_id, mime_type="image/png"),
            ],
        ),
    )
    await env.history.append(SESSION, _text("assistant", "a2"))
    rows = await env.rows()

    result = await fork_session(
        history=env.history,
        branches=env.branches,
        attachments=store,
        session_id=SESSION,
        anchor_row_id=rows[1].row_id,
    )
    forked = await env.history.load(result.session_id)
    assert [m.row_id for m in forked] == [m.row_id for m in rows[:2]]
    assert result.attachments_copied == 1
    assert store.read(result.session_id, kept.attachment_id)[0].endswith(b"kept")
    assert not store.exists(result.session_id, later.attachment_id)
    assert await env.rows() == rows
    assert await env.branches.list(result.session_id) == []
