"""History rewrite: anchors, branch isolation, verifier hooks."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from monkeybot.core.config.settings import VerifierTrackerConfig
from monkeybot.core.llm.provider import Message
from monkeybot.core.persistence.branches import BranchRecord
from monkeybot.core.persistence.goal_ledger import (
    Channel,
    GoalEntry,
    InMemoryGoalLedgerStore,
    Intent,
    Provenance,
    Status,
    first_dropped_seq,
)
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.persistence.thread_summary import (
    is_user_text_row,
    message_fingerprint,
)
from monkeybot.core.runtime.events import VerifierVerdict
from monkeybot.core.runtime.history_compaction import _is_pinned_history_row
from monkeybot.core.runtime.history_rewrite import (
    HistoryAnchor,
    HistoryRewriteError,
    RewriteEffects,
    activate_branch,
    assert_tool_pairs,
    branch_op,
    divergence_points,
    fork_session,
    load_active_history,
    locate_anchor,
    overlay_active_previews,
    prefix_end_for,
    purge_session_branches,
    truncate_active,
)
from monkeybot.core.types.content_blocks import SystemNotification, Text, ToolRequest, ToolResponse
from monkeybot.core.verifier.ledger import GoalLedger, statuses_without_dropped
from monkeybot.core.verifier.mailbox import VerdictMailbox
from monkeybot.core.verifier.tracker import ProgressTracker


def _user(text: str) -> Message:
    return Message(role="user", content=[Text(text=text)])


def _assistant(text: str) -> Message:
    return Message(role="assistant", content=[Text(text=text)])


def _anchor(messages: list[Message], index: int) -> HistoryAnchor:
    return HistoryAnchor(index, message_fingerprint(messages[index]))


@pytest.fixture
async def backend():
    store = SQLiteStorageBackend("sqlite:///:memory:")
    await store.open()
    try:
        yield store
    finally:
        await store.close()


async def _seed(backend: SQLiteStorageBackend, thread_id: str) -> list[Message]:
    messages = [
        _user("one"),
        _assistant("ack one"),
        _user("two"),
        _assistant("ack two"),
    ]
    for message in messages:
        await backend.history().append(thread_id, message)
    return messages


@pytest.mark.asyncio
async def test_anchor_mismatch_is_409(backend: SQLiteStorageBackend) -> None:
    await _seed(backend, "s1")
    messages = await backend.history().load("s1")
    anchor = HistoryAnchor(2, "a" * 64)
    with pytest.raises(HistoryRewriteError) as exc:
        await branch_op(
            history=backend.history(),
            branches=backend.branches(),
            session_id="s1",
            op="edit",
            anchor=anchor,
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "ANCHOR_MISMATCH"
    assert locate_anchor(messages, anchor) is None


@pytest.mark.asyncio
async def test_compacted_region_is_422(backend: SQLiteStorageBackend) -> None:
    summary = Message(
        role="assistant",
        content=[Text(text="[Context Summary]:\nolder turns")],
    )
    later = _user("still here")
    for message in (summary, later):
        await backend.history().append("s1", message)
    messages = await backend.history().load("s1")
    with pytest.raises(HistoryRewriteError) as exc:
        await branch_op(
            history=backend.history(),
            branches=backend.branches(),
            session_id="s1",
            op="rewind",
            anchor=_anchor(messages, 0),
        )
    assert exc.value.status_code == 422
    assert exc.value.code == "COMPACTED_REGION"
    result = await branch_op(
        history=backend.history(),
        branches=backend.branches(),
        session_id="s1",
        op="edit",
        anchor=_anchor(messages, 1),
    )
    copied = await backend.history().load(result.thread_id)
    assert copied == [summary]


@pytest.mark.asyncio
async def test_cut_keeps_tool_pairs_together() -> None:
    messages = [
        _user("run it"),
        Message(
            role="assistant",
            content=[ToolRequest(id="c1", name="load_file", args={"path": "a"})],
        ),
        Message(
            role="user",
            content=[
                ToolResponse(id="c1", tool_name="load_file", result=[Text(text="ok")]),
            ],
        ),
        _assistant("done"),
    ]
    with pytest.raises(HistoryRewriteError) as exc:
        assert_tool_pairs(messages, 2)
    assert exc.value.code == "TURN_BOUNDARY"
    # Rewind on the tool-call row keeps the matching result and the final text.
    assert prefix_end_for(messages, 1, "rewind") == 4
    # Editing the user prompt drops the tool turn entirely, so the pair stays whole.
    assert prefix_end_for(messages, 0, "edit") == 0
    assert not is_user_text_row(messages[1])


@pytest.mark.asyncio
async def test_rewind_on_user_row_stops_before_reply() -> None:
    messages = [_user("hi"), _assistant("there")]
    assert prefix_end_for(messages, 0, "edit") == 0
    assert prefix_end_for(messages, 0, "rewind") == 1
    assert prefix_end_for(messages, 1, "regenerate") == 0


@pytest.mark.asyncio
async def test_verdict_rows_are_copied_and_stay_pinned(backend: SQLiteStorageBackend) -> None:
    verdict = Message(
        role="system",
        content=[
            SystemNotification(
                notification_type="verifierVerdict",
                msg="on track",
                data={"status": "on_track"},
            )
        ],
    )
    messages = [_user("goal"), _assistant("working"), verdict, _user("next")]
    for message in messages:
        await backend.history().append("s1", message)
    stored = await backend.history().load("s1")
    result = await branch_op(
        history=backend.history(),
        branches=backend.branches(),
        session_id="s1",
        op="rewind",
        anchor=_anchor(stored, 1),
    )
    copied = await backend.history().load(result.thread_id)
    assert copied == messages[:3]
    assert _is_pinned_history_row(copied[-1])


@pytest.mark.asyncio
async def test_compacting_one_branch_leaves_sibling(backend: SQLiteStorageBackend) -> None:
    seeded = await _seed(backend, "s1")
    messages = await backend.history().load("s1")
    result = await branch_op(
        history=backend.history(),
        branches=backend.branches(),
        session_id="s1",
        op="rewind",
        anchor=_anchor(messages, 1),
    )
    sibling = await backend.history().load(result.thread_id)
    summary = Message(role="assistant", content=[Text(text="[Context Summary]:\ncompacted")])
    await backend.history().reset("s1", [summary])
    assert await backend.history().load("s1") == [summary]
    assert await backend.history().load(result.thread_id) == sibling
    assert sibling != [summary]
    threads = await backend.history().list_threads()
    assert [row.thread_id for row in threads] == ["s1"]
    assert seeded[0] == sibling[0]


@pytest.mark.asyncio
async def test_truncate_drops_tail_on_active_thread_only(backend: SQLiteStorageBackend) -> None:
    await _seed(backend, "s1")
    messages = await backend.history().load("s1")
    branched = await branch_op(
        history=backend.history(),
        branches=backend.branches(),
        session_id="s1",
        op="rewind",
        anchor=_anchor(messages, 3),
    )
    # Rewind of the last row keeps the whole thread; grow the branch past its
    # fork row, then truncate back to the new user message.
    for message in (_user("three"), _assistant("ack three")):
        await backend.history().append(branched.thread_id, message)
    active = await backend.history().load(branched.thread_id)
    await truncate_active(
        history=backend.history(),
        branches=backend.branches(),
        session_id="s1",
        anchor=_anchor(active, 4),
    )
    assert await backend.history().load(branched.thread_id) == active[:5]
    assert len(await backend.history().load("s1")) == 4


@pytest.mark.asyncio
async def test_fork_session_copies_prefix_without_branch_rows(
    backend: SQLiteStorageBackend,
) -> None:
    await _seed(backend, "s1")
    messages = await backend.history().load("s1")
    result = await fork_session(
        history=backend.history(),
        branches=backend.branches(),
        session_id="s1",
        anchor=_anchor(messages, 0),
    )
    assert result.forked_session_id is not None
    assert await backend.history().load(result.forked_session_id) == [messages[0]]
    assert await backend.history().load("s1") == messages
    assert await backend.branches().list("s1") == []


@pytest.mark.asyncio
async def test_tracker_fork_copies_workspace_and_resets_conversation() -> None:
    tracker = ProgressTracker(
        VerdictMailbox(),
        ledger_fn=lambda: None,
        config=VerifierTrackerConfig(),
    )
    state = tracker._state("src")
    state.files_read.add("a.py")
    state.write_counts["a.py"] = 2
    state.error_streak = 4
    state.latched.add("repeat")
    state.inner_turn = 3
    tracker.fork_thread("src", "dst")
    forked = tracker._by_thread["dst"]
    assert forked.files_read == {"a.py"}
    assert forked.write_counts == {"a.py": 2}
    assert forked.error_streak == 0
    assert forked.latched == set()
    assert forked.inner_turn == 0
    tracker.reset_conversation_state("src")
    assert state.error_streak == 0
    assert state.files_read == {"a.py"}


@pytest.mark.asyncio
async def test_ledger_copy_prefix_and_drop_after() -> None:
    store = InMemoryGoalLedgerStore()

    def entry(entry_id: str, seq: int, verbatim: str) -> GoalEntry:
        return GoalEntry(
            entry_id=entry_id,
            thread_id="src",
            seq=seq,
            verbatim=verbatim,
            provenance=Provenance.HUMAN,
            channel=Channel.MESSAGE,
            intent=Intent.NEW_GOAL,
            status=Status.ACTIVE,
            relates_to=None,
            constraints=(),
            done_when=(),
            created_at_ms=seq,
        )

    await store.append(entry("e1", 1, "one"))
    await store.append(entry("e2", 2, "two"))
    await store.append(entry("e3", 3, "three"))
    assert first_dropped_seq(await store.list_entries("src"), ["three"]) == 3
    copied = await store.copy_prefix("src", "dst", 2)
    assert copied == 2
    rows = await store.list_entries("dst")
    assert [row.verbatim for row in rows] == ["one", "two"]
    assert rows[0].entry_id != "e1"
    assert [row.seq for row in rows] == [1, 2]
    dropped = await store.drop_after("src", 1)
    assert dropped == 2
    assert [row.verbatim for row in await store.list_entries("src")] == ["one"]

    ledger = GoalLedger(store, classifier=object())  # type: ignore[arg-type]
    await ledger.copy_branch_prefix("dst", "other", ["two"])
    assert [row.verbatim for row in await store.list_entries("other")] == ["one"]
    assert ledger.resolved_intent("other") is not None


def _ledger_entry(entry_id: str, seq: int, verbatim: str, thread_id: str = "t") -> GoalEntry:
    return GoalEntry(
        entry_id=entry_id,
        thread_id=thread_id,
        seq=seq,
        verbatim=verbatim,
        provenance=Provenance.HUMAN,
        channel=Channel.MESSAGE,
        intent=Intent.NEW_GOAL,
        status=Status.ACTIVE,
        relates_to=None,
        constraints=(),
        done_when=(),
        created_at_ms=seq,
    )


def test_first_dropped_seq_survives_pruning_and_join_differences() -> None:
    # Ledger head pruned (no row for "one"); verbatim joins blocks with a space
    # while the stored row joins them with a newline.
    entries = [
        _ledger_entry("e2", 2, "two"),
        _ledger_entry("e3", 3, "noise"),
        _ledger_entry("e4", 4, "three part"),
        _ledger_entry("e5", 5, "four"),
    ]
    assert first_dropped_seq(entries, ["three\npart", "four"]) == 4
    # A dropped message with no ledger row is skipped, not a reason to stop.
    assert first_dropped_seq(entries, ["three part", "never classified", "four"]) == 4
    assert first_dropped_seq(entries, []) is None
    assert first_dropped_seq(entries, ["unrelated"]) is None


def test_first_dropped_seq_stops_at_a_kept_row() -> None:
    # The dropped "yes" never got a row; the older kept "yes" must not match.
    entries = [_ledger_entry("e1", 1, "yes"), _ledger_entry("e2", 2, "deploy")]
    assert first_dropped_seq(entries, ["yes"]) is None
    assert first_dropped_seq(entries, ["deploy", "yes"]) == 2


@pytest.mark.asyncio
async def test_ledger_truncate_without_match_keeps_rows() -> None:
    store = InMemoryGoalLedgerStore()
    for index, text in enumerate(("one", "two"), start=1):
        await store.append(_ledger_entry(f"e{index}", index, text))
    ledger = GoalLedger(store, classifier=object())  # type: ignore[arg-type]
    await ledger.drop_truncated("t", ["not in the ledger"])
    assert [row.verbatim for row in await store.list_entries("t")] == ["one", "two"]
    await ledger.drop_truncated("t", ["two"])
    assert [row.verbatim for row in await store.list_entries("t")] == ["one"]


def test_unpaired_tool_call_before_cut_does_not_block_rewrites() -> None:
    messages = [
        _user("run it"),
        Message(role="assistant", content=[ToolRequest(id="c1", name="terminal", args={})]),
        _user("never mind"),
        _assistant("ok"),
        _user("third"),
        _assistant("ack"),
    ]
    assert prefix_end_for(messages, 4, "edit") == 4
    assert prefix_end_for(messages, 5, "regenerate") == 4
    assert prefix_end_for(messages, 5, "rewind") == 6
    assert prefix_end_for(messages, 5, "fork") == 6


@pytest.mark.asyncio
async def test_truncate_refuses_to_cut_a_child_fork_point(
    backend: SQLiteStorageBackend,
) -> None:
    await _seed(backend, "s1")
    messages = await backend.history().load("s1")
    branched = await branch_op(
        history=backend.history(),
        branches=backend.branches(),
        session_id="s1",
        op="rewind",
        anchor=_anchor(messages, 3),
    )
    await activate_branch(branches=backend.branches(), session_id="s1", branch_id="root")
    with pytest.raises(HistoryRewriteError) as exc:
        await truncate_active(
            history=backend.history(),
            branches=backend.branches(),
            session_id="s1",
            anchor=_anchor(messages, 0),
        )
    assert exc.value.code == "BRANCHES_IN_TAIL"
    assert len(await backend.history().load("s1")) == 4
    assert await backend.history().load(branched.thread_id) == messages


@pytest.mark.asyncio
async def test_truncate_refuses_to_cut_own_fork_point(backend: SQLiteStorageBackend) -> None:
    await _seed(backend, "s1")
    messages = await backend.history().load("s1")
    branched = await branch_op(
        history=backend.history(),
        branches=backend.branches(),
        session_id="s1",
        op="edit",
        anchor=_anchor(messages, 2),
    )
    child = await backend.history().load(branched.thread_id)
    with pytest.raises(HistoryRewriteError) as exc:
        await truncate_active(
            history=backend.history(),
            branches=backend.branches(),
            session_id="s1",
            anchor=_anchor(child, 0),
        )
    assert exc.value.code == "BRANCHES_IN_TAIL"
    view = await load_active_history(backend.history(), backend.branches(), "s1")
    assert [point["options"] for point in view.branch_points] == [["root", branched.branch_id]]


@pytest.mark.asyncio
async def test_truncate_guard_ignores_earlier_duplicate_of_fork_row(
    backend: SQLiteStorageBackend,
) -> None:
    rows = [
        _user("a"),
        _assistant("Done."),
        _user("b"),
        _assistant("Done."),
        _user("c"),
        _assistant("x"),
    ]
    for message in rows:
        await backend.history().append("s1", message)
    messages = await backend.history().load("s1")
    await branch_op(
        history=backend.history(),
        branches=backend.branches(),
        session_id="s1",
        op="edit",
        anchor=_anchor(messages, 4),
    )
    await activate_branch(branches=backend.branches(), session_id="s1", branch_id="root")
    with pytest.raises(HistoryRewriteError) as exc:
        await truncate_active(
            history=backend.history(),
            branches=backend.branches(),
            session_id="s1",
            anchor=_anchor(messages, 2),
        )
    assert exc.value.code == "BRANCHES_IN_TAIL"
    view = await load_active_history(backend.history(), backend.branches(), "s1")
    assert [point["anchor"]["row_index"] for point in view.branch_points] == [3]


def test_locate_anchor_never_resolves_to_a_later_duplicate() -> None:
    messages = [_user("a"), _assistant("Done."), _user("b"), _assistant("Done.")]
    shifted = HistoryAnchor(2, message_fingerprint(messages[1]))
    assert locate_anchor(messages, shifted) == 1
    past_end = HistoryAnchor(9, message_fingerprint(messages[1]))
    assert locate_anchor(messages, past_end) == 3
    before_tail = HistoryAnchor(-1, message_fingerprint(messages[1]))
    assert locate_anchor(messages, before_tail) is None


@pytest.mark.asyncio
async def test_purge_keeps_branch_rows_when_a_thread_wipe_fails(
    backend: SQLiteStorageBackend,
) -> None:
    await _seed(backend, "s1")
    messages = await backend.history().load("s1")
    branched = await branch_op(
        history=backend.history(),
        branches=backend.branches(),
        session_id="s1",
        op="rewind",
        anchor=_anchor(messages, 1),
    )
    history = backend.history()
    real_reset = history.reset

    async def _reset(thread_id: str, rows: list[Message]) -> None:
        if thread_id == branched.thread_id:
            raise RuntimeError("boom")
        await real_reset(thread_id, rows)

    history.reset = _reset  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await purge_session_branches(history, backend.branches(), "s1")
    history.reset = real_reset  # type: ignore[method-assign]
    assert {row.branch_id for row in await backend.branches().list("s1")} == {
        "root",
        branched.branch_id,
    }
    await purge_session_branches(history, backend.branches(), "s1")
    assert await backend.history().load(branched.thread_id) == []
    assert await backend.branches().list("s1") == []


def _superseding_pair(thread_id: str) -> list[GoalEntry]:
    goal = replace(_ledger_entry("g1", 1, "build x", thread_id), status=Status.SUPERSEDED)
    pivot = replace(
        _ledger_entry("g2", 2, "actually do y", thread_id),
        intent=Intent.SCOPE_CHANGE,
        relates_to="g1",
    )
    return [goal, pivot]


@pytest.mark.asyncio
async def test_truncate_restores_goal_superseded_by_dropped_message() -> None:
    store = InMemoryGoalLedgerStore()
    for row in _superseding_pair("t"):
        await store.append(row)
    ledger = GoalLedger(store, classifier=object())  # type: ignore[arg-type]
    await ledger.drop_truncated("t", ["actually do y"])
    rows = await store.list_entries("t")
    assert [(row.verbatim, row.status) for row in rows] == [("build x", Status.ACTIVE)]


@pytest.mark.asyncio
async def test_branch_copy_restores_goal_without_touching_source() -> None:
    store = InMemoryGoalLedgerStore()
    for row in _superseding_pair("src"):
        await store.append(row)
    ledger = GoalLedger(store, classifier=object())  # type: ignore[arg-type]
    await ledger.copy_branch_prefix("src", "dst", ["actually do y"])
    copied = await store.list_entries("dst")
    assert [(row.verbatim, row.status) for row in copied] == [("build x", Status.ACTIVE)]
    source = await store.list_entries("src")
    assert [row.status for row in source] == [Status.SUPERSEDED, Status.ACTIVE]


def test_status_restore_keeps_a_kept_rows_change() -> None:
    goal, pivot = _superseding_pair("t")
    preempt = replace(
        _ledger_entry("g3", 3, "wait, first z", "t"),
        intent=Intent.PREEMPT,
        relates_to="g1",
    )
    deferred = replace(goal, status=Status.DEFERRED)
    assert statuses_without_dropped([deferred, pivot], [preempt]) == {"g1": Status.SUPERSEDED}


class _BlockedClassifier:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def classify(self, *_args: object, **_kwargs: object) -> object:
        await self.release.wait()
        raise RuntimeError("classifier unavailable")


@pytest.mark.asyncio
async def test_truncate_with_pending_classification_keeps_matching_kept_row() -> None:
    store = InMemoryGoalLedgerStore()
    await store.append(_ledger_entry("e1", 1, "yes"))
    await store.append(_ledger_entry("e2", 2, "do x"))
    classifier = _BlockedClassifier()
    ledger = GoalLedger(store, classifier=classifier)  # type: ignore[arg-type]
    ledger.admit("t", "yes", provenance=Provenance.HUMAN, channel=Channel.MESSAGE)

    async def _timeout(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError

    ledger.wait_idle = _timeout  # type: ignore[method-assign]
    await ledger.drop_truncated("t", ["yes"])
    assert [row.entry_id for row in await store.list_entries("t")] == ["e1", "e2"]
    classifier.release.set()
    await asyncio.wait_for(ledger._queues["t"].join(), timeout=1)
    assert [row.entry_id for row in await store.list_entries("t")] == ["e1", "e2"]
    ledger.close()


@pytest.mark.asyncio
async def test_branch_create_failure_removes_copied_thread(
    backend: SQLiteStorageBackend,
) -> None:
    await _seed(backend, "s1")
    messages = await backend.history().load("s1")
    branches = backend.branches()

    async def _fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    branches.create = _fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await branch_op(
            history=backend.history(),
            branches=branches,
            session_id="s1",
            op="rewind",
            anchor=_anchor(messages, 1),
        )
    cursor = await backend._conn.execute(  # type: ignore[union-attr]
        "SELECT COUNT(*) FROM conversation_history WHERE thread_id GLOB 'branch:*'"
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None and row[0] == 0


@pytest.mark.asyncio
async def test_load_active_history_tail_keeps_absolute_indexes(
    backend: SQLiteStorageBackend,
) -> None:
    await _seed(backend, "s1")
    view = await load_active_history(backend.history(), backend.branches(), "s1", limit=2)
    assert view.offset == 2
    assert [message.content[0].text for message in view.messages] == [  # type: ignore[union-attr]
        "two",
        "ack two",
    ]


@pytest.mark.asyncio
async def test_preview_overlay_keeps_rows_when_branch_lookup_fails(
    backend: SQLiteStorageBackend,
) -> None:
    await _seed(backend, "s1")
    rows = await backend.history().list_threads()
    branches = backend.branches()

    async def _fail(_session_ids: object) -> dict[str, object]:
        raise RuntimeError("index missing")

    branches.active_non_root = _fail  # type: ignore[method-assign]
    assert await overlay_active_previews(backend.history(), branches, rows) == rows


@pytest.mark.asyncio
async def test_late_verdict_after_rewrite_is_dropped(backend: SQLiteStorageBackend) -> None:
    await _seed(backend, "s1")
    messages = await backend.history().load("s1")
    mailbox = VerdictMailbox()
    mailbox.open_request("s1", "req-1")
    mailbox.clear_request("s1", "req-1")
    await branch_op(
        history=backend.history(),
        branches=backend.branches(),
        session_id="s1",
        op="rewind",
        anchor=_anchor(messages, 1),
        effects=RewriteEffects(tracker=None, ledger=None),
    )
    accepted = mailbox.put(
        "s1",
        VerifierVerdict(request_id="req-1", verdict_id="v1", checkpoint_id="req-1:1"),
    )
    assert accepted is False


def _branch(
    branch_id: str,
    parent: str | None,
    fork_row: int | None,
    thread: list[Message],
) -> BranchRecord:
    return BranchRecord(
        branch_id=branch_id,
        session_id="s1",
        thread_id=f"t-{branch_id}",
        parent_branch_id=parent,
        fork_row_index=fork_row,
        fork_fingerprint=None if fork_row is None else message_fingerprint(thread[fork_row]),
        op=None if parent is None else "edit",
        created_at=len(branch_id),
        last_active_at=0,
        is_active=False,
    )


def _nested_branches(
    grandchild_fork_row: int,
) -> tuple[list[BranchRecord], list[Message]]:
    """Root, child forked at row 5, grandchild forked from the child.

    Rows 1 and 5 have identical content, so a fork fingerprint alone cannot
    tell them apart.
    """
    root = [
        _user("a"),
        _assistant("Done."),
        _user("b"),
        _assistant("x"),
        _user("c"),
        _assistant("Done."),
        _user("d"),
        _assistant("y"),
    ]
    child = [*root[:6], _user("d2"), _assistant("z")]
    grandchild = [*child[: grandchild_fork_row + 1], _user("new"), _assistant("w")]
    records = [
        _branch("root", None, None, root),
        _branch("child", "root", 5, root),
        _branch("grandchild", "child", grandchild_fork_row, child),
    ]
    return records, grandchild


def test_divergence_points_skip_a_fork_row_the_branch_never_inherited() -> None:
    records, grandchild = _nested_branches(grandchild_fork_row=1)
    points = divergence_points(records, "grandchild", grandchild)
    assert [(p["anchor"]["row_index"], p["options"]) for p in points] == [
        (1, ["child", "grandchild"]),
    ]


def test_divergence_points_keep_an_inherited_ancestor_fork_row() -> None:
    records, grandchild = _nested_branches(grandchild_fork_row=6)
    points = divergence_points(records, "grandchild", grandchild)
    assert [(p["anchor"]["row_index"], p["options"]) for p in points] == [
        (5, ["root", "child"]),
        (6, ["child", "grandchild"]),
    ]
