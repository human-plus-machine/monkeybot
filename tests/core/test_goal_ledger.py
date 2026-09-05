"""Goal ledger classification, lifecycle, steer tap, and settlement-latency tests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookManager
from monkeybot.core.llm.provider import Done, Message, ProviderEvent, TextDelta, ToolCall
from monkeybot.core.persistence.goal_ledger import (
    Channel,
    Classification,
    Constraint,
    ConstraintDraft,
    ConstraintKind,
    GoalEntry,
    InMemoryGoalLedgerStore,
    Intent,
    Provenance,
    Status,
    new_entry_id,
    now_ms,
    resolve_intent,
)
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.runtime.input_admission import InputAdmission
from monkeybot.core.runtime.loop import run
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.core.verifier import ScriptedClassifier
from monkeybot.core.verifier.classify import parse_classification
from monkeybot.core.verifier.ledger import GoalLedger, format_compaction_facts
from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider, RecordingExecutor
from tests.core.test_loop import _ctx as loop_ctx


def _entry(
    *,
    verbatim: str,
    intent: Intent = Intent.NEW_GOAL,
    status: Status = Status.ACTIVE,
    provenance: Provenance = Provenance.HUMAN,
    channel: Channel | None = Channel.MESSAGE,
    relates_to: str | None = None,
    constraints: tuple[Constraint, ...] = (),
    seq: int = 1,
    entry_id: str | None = None,
) -> GoalEntry:
    return GoalEntry(
        entry_id=entry_id or new_entry_id(),
        thread_id="t1",
        seq=seq,
        verbatim=verbatim,
        provenance=provenance,
        channel=channel,
        intent=intent,
        status=status,
        relates_to=relates_to,
        constraints=constraints,
        done_when=(),
        created_at_ms=now_ms(),
    )


def test_preempt_defers_rather_than_abandons() -> None:
    goal = _entry(verbatim="ship the API", seq=1)
    preempt = _entry(
        verbatim="do the hotfix first",
        intent=Intent.PREEMPT,
        status=Status.ACTIVE,
        relates_to=goal.entry_id,
        seq=2,
    )
    deferred_goal = _entry(
        verbatim=goal.verbatim,
        intent=goal.intent,
        status=Status.DEFERRED,
        seq=1,
        entry_id=goal.entry_id,
    )
    view = resolve_intent([deferred_goal, preempt], pending_classification=False)
    assert view.active_goal is not None
    assert view.active_goal.intent == Intent.PREEMPT
    assert len(view.deferred_stack) == 1
    assert view.deferred_stack[0].entry_id == goal.entry_id
    assert view.deferred_stack[0].status == Status.DEFERRED


def test_scope_change_supersedes() -> None:
    old = _entry(verbatim="write tests", seq=1, status=Status.SUPERSEDED)
    new = _entry(
        verbatim="write the docs instead",
        intent=Intent.SCOPE_CHANGE,
        relates_to=old.entry_id,
        seq=2,
    )
    view = resolve_intent([old, new], pending_classification=False)
    assert view.active_goal is not None
    assert view.active_goal.entry_id == new.entry_id
    assert any("write tests" in line for line in view.superseded)


def test_constraints_accumulate_across_goal_changes() -> None:
    c1 = Constraint(
        kind=ConstraintKind.PATH_GLOB,
        pattern="db/migrations/**",
        source_entry_id="e1",
        verbatim="don't touch migrations",
    )
    old = _entry(verbatim="refactor auth", seq=1, status=Status.SUPERSEDED, constraints=(c1,))
    new = _entry(
        verbatim="refactor billing",
        intent=Intent.SCOPE_CHANGE,
        relates_to=old.entry_id,
        seq=2,
    )
    view = resolve_intent([old, new], pending_classification=False)
    assert len(view.standing_constraints) == 1
    assert view.standing_constraints[0].pattern == "db/migrations/**"


def test_verifier_steer_excluded_from_intent() -> None:
    human = _entry(verbatim="ship the API", seq=1)
    steer = _entry(
        verbatim="you are drifting",
        provenance=Provenance.VERIFIER_STEER,
        channel=Channel.STEER,
        intent=Intent.NOISE,
        status=Status.SATISFIED,
        seq=2,
    )
    view = resolve_intent([human, steer], pending_classification=False)
    assert view.active_goal is not None
    assert view.active_goal.entry_id == human.entry_id
    assert view.active_goal.verbatim == "ship the API"


def test_parse_classification_json_and_fences() -> None:
    parsed = parse_classification(
        '```json\n{"intent":"correction","relates_to":null,'
        '"constraints":[{"kind":"path_glob","pattern":"src/**","verbatim":"leave src alone"}],'
        '"done_when":[]}\n```'
    )
    assert parsed is not None
    assert parsed.intent == Intent.CORRECTION
    assert parsed.constraints[0].kind == ConstraintKind.PATH_GLOB


@pytest.mark.asyncio
async def test_sqlite_goal_ledger_roundtrip() -> None:
    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    try:
        store = backend.goal_ledger()
        seq = await store.next_seq("t1")
        entry = _entry(verbatim="hello", seq=seq)
        await store.append(entry)
        loaded = await store.list_entries("t1")
        assert len(loaded) == 1
        assert loaded[0].verbatim == "hello"
        await store.update_status(entry.entry_id, Status.DEFERRED)
        loaded = await store.list_entries("t1")
        assert loaded[0].status == Status.DEFERRED
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_two_rapid_inputs_classified_in_seq_order() -> None:
    store = InMemoryGoalLedgerStore()
    classifier = ScriptedClassifier(
        [
            Classification(intent=Intent.NEW_GOAL, relates_to=None, constraints=(), done_when=()),
            Classification(
                intent=Intent.REFINEMENT,
                relates_to=None,
                constraints=(),
                done_when=(),
            ),
        ]
    )
    ledger = GoalLedger(store, classifier)
    ledger.admit("t1", "first", provenance=Provenance.HUMAN, channel=Channel.MESSAGE)
    ledger.admit("t1", "second", provenance=Provenance.HUMAN, channel=Channel.MESSAGE)
    await ledger.wait_idle("t1")
    assert classifier.calls == ["first", "second"]
    entries = await store.list_entries("t1")
    assert [e.seq for e in entries] == [1, 2]
    assert [e.verbatim for e in entries] == ["first", "second"]
    ledger.close()


@pytest.mark.asyncio
async def test_preempt_and_scope_change_lifecycle() -> None:
    store = InMemoryGoalLedgerStore()
    classifier = ScriptedClassifier(
        [
            Classification(intent=Intent.NEW_GOAL, relates_to=None, constraints=(), done_when=()),
            Classification(intent=Intent.PREEMPT, relates_to=None, constraints=(), done_when=()),
            Classification(intent=Intent.SCOPE_CHANGE, relates_to=None, constraints=(), done_when=()),
        ]
    )
    ledger = GoalLedger(store, classifier)
    ledger.admit("t1", "goal A", provenance=Provenance.HUMAN, channel=Channel.MESSAGE)
    await ledger.wait_idle("t1")
    ledger.admit("t1", "do B first", provenance=Provenance.HUMAN, channel=Channel.MESSAGE)
    await ledger.wait_idle("t1")
    view = ledger.resolved_intent("t1")
    assert view is not None
    assert view.active_goal is not None
    assert view.active_goal.intent == Intent.PREEMPT
    assert len(view.deferred_stack) == 1
    ledger.admit("t1", "actually do C", provenance=Provenance.HUMAN, channel=Channel.MESSAGE)
    await ledger.wait_idle("t1")
    view = ledger.resolved_intent("t1")
    assert view is not None
    assert view.active_goal is not None
    assert view.active_goal.intent == Intent.SCOPE_CHANGE
    ledger.close()


@pytest.mark.asyncio
async def test_human_steer_derives_intent() -> None:
    store = InMemoryGoalLedgerStore()
    classifier = ScriptedClassifier(
        [
            Classification(intent=Intent.NEW_GOAL, relates_to=None, constraints=(), done_when=()),
            Classification(
                intent=Intent.CORRECTION,
                relates_to=None,
                constraints=(
                    ConstraintDraft(
                        kind=ConstraintKind.PATH_GLOB,
                        pattern="db/migrations/**",
                        verbatim="leave the migrations alone",
                    ),
                ),
                done_when=(),
            ),
        ]
    )
    ledger = GoalLedger(store, classifier)
    mgr = HookManager()
    ledger.register(mgr)
    admission = InputAdmission()

    class SteerOnExecute:
        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
            del call, ctx
            admission.enqueue_steer([Text(text="leave the migrations alone")])
            return ToolExecutionResult.ok_text("ok")

    ctx = loop_ctx()
    ctx = TurnContext(
        **{
            **ctx.__dict__,
            "tools": [ToolDef("read_file", "r", {"type": "object"}, parallel_safe=True)],
            "goal_ledger": ledger,
        }
    )
    async for _ in run(
        "ship the API",
        ctx,
        provider=FakeProvider(
            [
                [ToolCall(call_id="c1", name="read_file", args={"path": "x"}), Done()],
                [TextDelta(text="done"), Done()],
            ]
        ),
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=SteerOnExecute(),
        max_turns=4,
        hook_manager=mgr,
        input_admission=admission,
    ):
        pass
    await ledger.wait_idle(ctx.thread_id)
    entries = await store.list_entries(ctx.thread_id)
    steer_entries = [e for e in entries if e.channel == Channel.STEER]
    assert len(steer_entries) == 1
    assert steer_entries[0].provenance == Provenance.HUMAN
    view = ledger.resolved_intent(ctx.thread_id)
    assert view is not None
    assert any(c.kind == ConstraintKind.PATH_GLOB for c in view.standing_constraints)
    ledger.close()


@pytest.mark.asyncio
async def test_verifier_steer_does_not_change_active_goal() -> None:
    store = InMemoryGoalLedgerStore()
    classifier = ScriptedClassifier(
        [Classification(intent=Intent.NEW_GOAL, relates_to=None, constraints=(), done_when=())]
    )
    ledger = GoalLedger(store, classifier)
    mgr = HookManager()
    ledger.register(mgr)
    admission = InputAdmission()
    ctx = loop_ctx()
    ctx = TurnContext(
        **{
            **ctx.__dict__,
            "tools": [ToolDef("read_file", "r", {"type": "object"}, parallel_safe=True)],
            "goal_ledger": ledger,
        }
    )

    class EnqueueVerifierSteer:
        async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
            del call, ctx
            admission.enqueue_steer(
                [Text(text="verifier: stay on the original goal")],
                provenance="verifier_steer",
            )
            return ToolExecutionResult.ok_text("ok")

    async for _ in run(
        "ship the API",
        ctx,
        provider=FakeProvider(
            [
                [ToolCall(call_id="c1", name="read_file", args={"path": "x"}), Done()],
                [TextDelta(text="done"), Done()],
            ]
        ),
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=EnqueueVerifierSteer(),
        max_turns=4,
        hook_manager=mgr,
        input_admission=admission,
    ):
        pass
    await ledger.wait_idle(ctx.thread_id)
    view = ledger.resolved_intent(ctx.thread_id)
    assert view is not None
    assert view.active_goal is not None
    assert view.active_goal.verbatim == "ship the API"
    entries = await store.list_entries(ctx.thread_id)
    assert any(e.provenance == Provenance.VERIFIER_STEER for e in entries)
    ledger.close()


@pytest.mark.asyncio
async def test_slow_classifier_does_not_delay_first_provider_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = InMemoryGoalLedgerStore()
    classifier = ScriptedClassifier(
        [Classification(intent=Intent.NEW_GOAL, relates_to=None, constraints=(), done_when=())],
        delay_s=5.0,
    )
    ledger = GoalLedger(store, classifier)
    mgr = HookManager()
    ledger.register(mgr)
    ctx = loop_ctx()
    ctx = TurnContext(**{**ctx.__dict__, "goal_ledger": ledger})
    first_stream_at: list[float] = []
    loop = asyncio.get_running_loop()
    t0 = loop.time()

    class TimingProvider:
        @property
        def name(self) -> str:
            return "fake"

        @property
        def supports_streaming(self) -> bool:
            return True

        async def stream(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDef],
            *,
            model: str,
            thinking_budget: int | None = None,
        ) -> AsyncIterator[ProviderEvent]:
            del messages, tools, model, thinking_budget
            if not first_stream_at:
                first_stream_at.append(loop.time() - t0)
            yield TextDelta(text="ok")
            yield Done()

        async def count_input_tokens(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDef],
            *,
            model: str,
            thinking_budget: int | None = None,
        ) -> int:
            del messages, tools, model, thinking_budget
            return 0

    caplog.set_level(logging.WARNING, logger="monkeybot.core.hooks")
    async for _ in run(
        "hello",
        ctx,
        provider=TimingProvider(),
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=2,
        hook_manager=mgr,
    ):
        pass
    assert first_stream_at
    assert first_stream_at[0] < 1.0
    assert not any("settlement timed out" in r.message for r in caplog.records)
    ledger.close()


def test_compaction_facts_include_objective_and_constraints() -> None:
    constraint = Constraint(
        kind=ConstraintKind.PATH_GLOB,
        pattern="db/migrations/**",
        source_entry_id="e1",
        verbatim="don't touch migrations",
    )
    goal = _entry(verbatim="refactor billing", constraints=(constraint,))
    view = resolve_intent([goal], pending_classification=False)
    facts = format_compaction_facts(view)
    assert "refactor billing" in facts
    assert "db/migrations/**" in facts
    assert "authoritative" in facts
