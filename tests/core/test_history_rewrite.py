"""History rewrite: anchors, branch isolation, verifier hooks."""

from __future__ import annotations

import pytest

from monkeybot.core.config.settings import VerifierTrackerConfig
from monkeybot.core.llm.provider import Message
from monkeybot.core.persistence.goal_ledger import (
    Channel,
    GoalEntry,
    InMemoryGoalLedgerStore,
    Intent,
    Provenance,
    Status,
    match_verbatim_seq,
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
    assert_tool_pairs,
    branch_op,
    fork_session,
    locate_anchor,
    prefix_end_for,
    truncate_active,
)
from monkeybot.core.types.content_blocks import SystemNotification, Text, ToolRequest, ToolResponse
from monkeybot.core.verifier.ledger import GoalLedger
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
    # Rewind of the last row keeps the whole thread, then truncate the source
    # via the new active branch back to the first user message.
    active = await backend.history().load(branched.thread_id)
    await truncate_active(
        history=backend.history(),
        branches=backend.branches(),
        session_id="s1",
        anchor=_anchor(active, 0),
    )
    assert await backend.history().load(branched.thread_id) == [active[0]]
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
    assert match_verbatim_seq(await store.list_entries("src"), ["one", "two"]) == 2
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
    await ledger.copy_matched_prefix("dst", "other", ["one"])
    assert [row.verbatim for row in await store.list_entries("other")] == ["one"]
    assert ledger.resolved_intent("other") is not None


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
