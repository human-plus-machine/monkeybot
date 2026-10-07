"""Goal ledger and progress tracker carry-over across history rewrites."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from monkeybot.core.config.settings import VerifierTrackerConfig
from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookManager
from monkeybot.core.llm.provider import Done, Message, TextDelta, ToolCall
from monkeybot.core.persistence.branches import SQLiteBranchStore
from monkeybot.core.persistence.goal_ledger import (
    Channel,
    Classification,
    ConstraintDraft,
    ConstraintKind,
    GoalLedgerStore,
    InMemoryGoalLedgerStore,
    Intent,
    Provenance,
    SQLiteGoalLedgerStore,
    Status,
)
from monkeybot.core.persistence.history import SQLiteHistoryStore
from monkeybot.core.persistence.sqlite import apply_schema, open_connection
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.runtime.history_rewrite import (
    branch_op,
    purge_session_branches,
    truncate_active,
)
from monkeybot.core.runtime.input_admission import InputAdmission
from monkeybot.core.runtime.loop import run
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.core.verifier.ledger import GoalLedger
from monkeybot.core.verifier.mailbox import VerdictMailbox
from monkeybot.core.verifier.rewrite_effects import VerifierRewriteEffects
from monkeybot.core.verifier.tracker import ProgressTracker
from tests.core.test_goal_ledger import ScriptedClassifier
from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider
from tests.core.test_loop import _ctx as loop_ctx

SRC = "src-thread"
DST = "dst-thread"


def _goal(intent: Intent, *constraints: ConstraintDraft) -> Classification:
    return Classification(intent=intent, relates_to=None, constraints=constraints)


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[GoalLedgerStore]:
    if request.param == "memory":
        yield InMemoryGoalLedgerStore()
        return
    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    yield backend.goal_ledger()
    await backend.close()


async def _seeded(store: GoalLedgerStore, *intents: Classification) -> GoalLedger:
    """A ledger with one HUMAN entry per intent, from rows r1, r2, ... on SRC."""
    ledger = GoalLedger(store, ScriptedClassifier(list(intents)))
    for index in range(len(intents)):
        ledger.admit(
            SRC,
            f"message {index + 1}",
            provenance=Provenance.HUMAN,
            channel=Channel.MESSAGE,
            source_row_id=f"r{index + 1}",
        )
    await ledger.wait_idle(SRC)
    return ledger


_KEEP_OUT = ConstraintDraft(kind=ConstraintKind.PATH_GLOB, pattern="db/**", verbatim="not db")


@pytest.mark.asyncio
async def test_copy_branch_keeps_prefix_remaps_ids_and_undoes_dropped_status(store) -> None:
    ledger = await _seeded(
        store,
        _goal(Intent.NEW_GOAL),
        _goal(Intent.CORRECTION, _KEEP_OUT),
        _goal(Intent.NEW_GOAL),
    )
    source = await store.list_entries(SRC)
    assert source[0].status == Status.SUPERSEDED

    assert await ledger.copy_branch(SRC, DST, {"r3", "assistant-row"}) == 2
    goal, correction = await store.list_entries(DST)
    assert [goal.seq, correction.seq] == [1, 2]
    assert [goal.source_row_id, correction.source_row_id] == ["r1", "r2"]
    assert goal.status == Status.ACTIVE
    assert goal.entry_id != source[0].entry_id
    assert correction.relates_to == goal.entry_id
    assert correction.constraints[0].source_entry_id == correction.entry_id
    view = ledger.resolved_intent(DST)
    assert view is not None and view.active_goal is not None
    assert view.active_goal.verbatim == "message 1"
    assert await store.list_entries(SRC) == source
    ledger.close()


@pytest.mark.asyncio
async def test_existing_goal_ledger_table_gains_source_row_id() -> None:
    conn = await open_connection("sqlite:///:memory:")
    await conn.execute(
        """CREATE TABLE goal_ledger (
        entry_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, seq INTEGER NOT NULL,
        verbatim TEXT NOT NULL, provenance TEXT NOT NULL, channel TEXT,
        intent TEXT NOT NULL, status TEXT NOT NULL, relates_to TEXT,
        constraints_json TEXT NOT NULL, done_when_json TEXT NOT NULL,
        created_at_ms INTEGER NOT NULL)"""
    )
    await conn.execute(
        "INSERT INTO goal_ledger VALUES ('e1', 't', 1, 'old', 'human', NULL,"
        " 'new_goal', 'active', NULL, '[]', '[]', 1)"
    )
    await conn.commit()
    await apply_schema(conn)
    [legacy] = await SQLiteGoalLedgerStore(conn).list_entries("t")
    assert (legacy.verbatim, legacy.source_row_id) == ("old", None)
    await conn.close()


@pytest.mark.asyncio
async def test_copy_branch_keeps_entries_whose_rows_were_compacted_away(store) -> None:
    ledger = await _seeded(store, _goal(Intent.NEW_GOAL), _goal(Intent.REFINEMENT))
    # Neither r1 nor r2 is on the thread any more; nothing was dropped.
    assert await ledger.copy_branch(SRC, DST, {"summary-tail"}) == 2
    ledger.close()


@pytest.mark.asyncio
async def test_drop_rows_truncates_and_restores_status(store) -> None:
    ledger = await _seeded(store, _goal(Intent.NEW_GOAL), _goal(Intent.PREEMPT))
    assert (await store.list_entries(SRC))[0].status == Status.DEFERRED
    assert await ledger.drop_rows(SRC, {"r2"}) == 1
    [goal] = await store.list_entries(SRC)
    assert goal.status == Status.ACTIVE
    assert await ledger.drop_rows(SRC, {"not-classified"}) == 0
    ledger.close()


@pytest.mark.asyncio
async def test_queued_classification_for_a_dropped_row_is_skipped() -> None:
    store = InMemoryGoalLedgerStore()
    classifier = ScriptedClassifier([], delay_s=0.05)
    ledger = GoalLedger(store, classifier)
    for row in ("r1", "r2"):
        ledger.admit(
            SRC, row, provenance=Provenance.HUMAN, channel=Channel.MESSAGE, source_row_id=row
        )
    await ledger.drop_rows(SRC, {"r2"})
    assert classifier.calls == ["r1"]
    assert [e.source_row_id for e in await store.list_entries(SRC)] == ["r1"]
    ledger.close()


@pytest.mark.asyncio
async def test_clear_thread_deletes_rows_and_skips_queued_jobs() -> None:
    store = InMemoryGoalLedgerStore()
    classifier = ScriptedClassifier([], delay_s=0.05)
    ledger = GoalLedger(store, classifier)
    for row in ("r1", "r2"):
        ledger.admit(
            SRC, row, provenance=Provenance.HUMAN, channel=Channel.MESSAGE, source_row_id=row
        )
    await ledger.clear_thread(SRC)
    assert classifier.calls == []
    assert await store.list_entries(SRC) == []
    assert ledger.resolved_intent(SRC) is None
    ledger.admit(SRC, "r3", provenance=Provenance.HUMAN, channel=Channel.MESSAGE)
    await ledger.wait_idle(SRC)
    assert [e.verbatim for e in await store.list_entries(SRC)] == ["r3"]
    ledger.close()


def test_tracker_fork_keeps_workspace_and_resets_turn_state() -> None:
    tracker = ProgressTracker(
        VerdictMailbox(), ledger_fn=lambda: None, config=VerifierTrackerConfig()
    )
    state = tracker._state(SRC)
    state.files_read.add("a.py")
    state.write_counts["a.py"] = 3
    state.error_streak = 4
    tracker.fork_thread(SRC, DST)
    forked = tracker._state(DST)
    assert (forked.files_read, forked.write_counts, forked.error_streak) == (
        {"a.py"},
        {"a.py": 3},
        0,
    )
    forked.files_read.add("b.py")
    assert tracker._state(SRC).files_read == {"a.py"}

    tracker.reset_conversation_state(SRC)
    assert tracker._state(SRC).error_streak == 0
    assert tracker._state(SRC).write_counts == {"a.py": 3}
    tracker.forget(SRC)
    assert tracker._state(SRC).files_read == set()


@pytest.mark.asyncio
async def test_turn_loop_records_history_row_ids_on_ledger_entries() -> None:
    store = InMemoryGoalLedgerStore()
    ledger = GoalLedger(store, ScriptedClassifier([]))
    mgr = HookManager()
    ledger.register(mgr)
    admission = InputAdmission()

    class SteerOnExecute:
        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
            del call, ctx
            admission.enqueue_steer([Text(text="also do this")])
            return ToolExecutionResult.ok_text("ok")

    base = loop_ctx()
    ctx = TurnContext(
        **{
            **base.__dict__,
            "tools": [ToolDef("read_file", "r", {"type": "object"}, parallel_safe=True)],
            "goal_ledger": ledger,
        }
    )
    history = FakeHistory()
    async for _ in run(
        "ship it",
        ctx,
        provider=FakeProvider(
            [
                [ToolCall(call_id="c1", name="read_file", args={"path": "x"}), Done()],
                [TextDelta(text="done"), Done()],
            ]
        ),
        history=history,
        inspectors=[AllowInspector()],
        tool_executor=SteerOnExecute(),
        max_turns=4,
        hook_manager=mgr,
        input_admission=admission,
    ):
        pass
    await ledger.wait_idle(ctx.thread_id)
    user_rows = {
        m.row_id: m for m in history.rows if m.role == "user" and isinstance(m.content[0], Text)
    }
    entries = await store.list_entries(ctx.thread_id)
    assert [e.verbatim for e in entries] == ["ship it", "also do this"]
    assert all(e.source_row_id in user_rows for e in entries)
    ledger.close()


class _Effects:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail = fail

    async def branched(self, source: str, target: str, dropped: Any) -> None:
        self.calls.append(("branched", source, target, set(dropped)))
        if self.fail:
            raise RuntimeError("ledger down")

    async def truncated(self, thread_id: str, dropped: Any) -> None:
        self.calls.append(("truncated", thread_id, set(dropped)))

    async def purged(self, thread_ids: Any) -> None:
        self.calls.append(("purged", set(thread_ids)))


@pytest_asyncio.fixture
async def stores() -> AsyncIterator[tuple[SQLiteHistoryStore, SQLiteBranchStore]]:
    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    yield backend.history(), backend.branches()
    await backend.close()


async def _seed(history: SQLiteHistoryStore, *texts: str) -> list[Message]:
    for index, text in enumerate(texts):
        role = "user" if index % 2 == 0 else "assistant"
        await history.append("s1", Message(role=role, content=[Text(text=text)]))  # type: ignore[arg-type]
    return await history.load("s1")


@pytest.mark.asyncio
async def test_rewrites_report_dropped_rows_and_survive_effect_failures(stores) -> None:
    history, branches = stores
    rows = await _seed(history, "u1", "a1", "u2", "a2")
    failing = _Effects(fail=True)
    result = await branch_op(
        history=history,
        branches=branches,
        session_id="s1",
        op="edit",
        anchor_row_id=rows[2].row_id,  # type: ignore[arg-type]
        effects=failing,
    )
    assert failing.calls == [("branched", "s1", result.thread_id, {rows[2].row_id, rows[3].row_id})]
    assert (await branches.get_active("s1")).branch_id == result.branch_id  # type: ignore[union-attr]

    effects = _Effects()
    await history.append(result.thread_id, Message(role="user", content=[Text(text="v2")]))
    await history.append(result.thread_id, Message(role="assistant", content=[Text(text="b2")]))
    branch_rows = await history.load(result.thread_id)
    await truncate_active(
        history=history,
        branches=branches,
        session_id="s1",
        anchor_row_id=branch_rows[2].row_id,  # type: ignore[arg-type]
        effects=effects,
    )
    await purge_session_branches(history, branches, "s1", effects=effects)
    assert effects.calls == [
        ("truncated", result.thread_id, {branch_rows[3].row_id}),
        ("purged", {"s1", result.thread_id}),
    ]


@pytest.mark.asyncio
async def test_verifier_effects_carry_ledger_onto_branch_thread(stores) -> None:
    history, branches = stores
    rows = await _seed(history, "u1", "a1", "u2", "a2")
    store = InMemoryGoalLedgerStore()
    ledger = GoalLedger(store, ScriptedClassifier([_goal(Intent.NEW_GOAL), _goal(Intent.NEW_GOAL)]))
    for row in (rows[0], rows[2]):
        ledger.admit(
            "s1",
            row.content[0].text,  # type: ignore[union-attr]
            provenance=Provenance.HUMAN,
            channel=Channel.MESSAGE,
            source_row_id=row.row_id,
        )
    await ledger.wait_idle("s1")
    result = await branch_op(
        history=history,
        branches=branches,
        session_id="s1",
        op="edit",
        anchor_row_id=rows[2].row_id,  # type: ignore[arg-type]
        effects=VerifierRewriteEffects(ledger=ledger, tracker=None),
    )
    [carried] = await store.list_entries(result.thread_id)
    assert (carried.verbatim, carried.status) == ("u1", Status.ACTIVE)
    await purge_session_branches(
        history, branches, "s1", effects=VerifierRewriteEffects(ledger=ledger, tracker=None)
    )
    assert await store.list_entries("s1") == []
    assert await store.list_entries(result.thread_id) == []
    ledger.close()
