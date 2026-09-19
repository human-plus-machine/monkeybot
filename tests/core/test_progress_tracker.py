"""Constraint matching and ProgressTracker cold-state / signal tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from monkeybot.core.config.settings import VerifierTrackerConfig
from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookPayload
from monkeybot.core.persistence.goal_ledger import (
    Channel,
    Classification,
    Constraint,
    ConstraintDraft,
    ConstraintKind,
    InMemoryGoalLedgerStore,
    Intent,
    Provenance,
)
from monkeybot.core.runtime.events import (
    Error,
    SystemPromptSnapshot,
    ToolCallResult,
    TurnComplete,
    VerifierVerdict,
)
from monkeybot.core.runtime.loop import run
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.core.verifier.ledger import GoalLedger
from monkeybot.core.verifier.mailbox import VerdictMailbox
from monkeybot.core.verifier.match import constraint_matches, glob_match
from monkeybot.core.verifier.tracker import ProgressTracker
from tests.core.test_goal_ledger import ScriptedClassifier
from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider, RecordingExecutor
from tests.core.test_loop import _ctx as loop_ctx


def _ctx() -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[ToolDef("write_file", "w", {})],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )


def _payload(
    *,
    event: HookEvent,
    tool_name: str | None = None,
    tool_args: dict | None = None,
    tool_error: str | None = None,
    inner_turn: int | None = 3,
    assistant_text: str | None = None,
    tool_requests: list | None = None,
    usage: dict | None = None,
) -> HookPayload:
    return HookPayload(
        event=event,
        thread_id="t1",
        request_id="r1",
        ctx=_ctx(),
        tool_name=tool_name,
        tool_args=tool_args,
        tool_error=tool_error,
        inner_turn=inner_turn,
        assistant_text=assistant_text,
        tool_requests=tool_requests,
        usage=usage,
    )


def test_path_glob_matches_nested_and_skips_unrelated() -> None:
    constraint = Constraint(
        kind=ConstraintKind.PATH_GLOB,
        pattern="db/migrations/**",
        source_entry_id="e1",
        verbatim="leave migrations",
    )
    assert constraint_matches(
        constraint, tool_name="write_file", args={"path": "db/migrations/001.sql"}
    )
    assert constraint_matches(
        constraint,
        tool_name="write_file",
        args={"paths": ["db/migrations/002.sql"]},
    )
    assert not constraint_matches(
        constraint, tool_name="write_file", args={"path": "src/models.py"}
    )
    assert not constraint_matches(
        constraint, tool_name="write_file", args={"content": "db/migrations/001.sql"}
    )


def test_tool_name_and_command_regex_and_free_text() -> None:
    tool_c = Constraint(
        kind=ConstraintKind.TOOL_NAME,
        pattern="write_file",
        source_entry_id="e1",
        verbatim="don't write",
    )
    cmd_c = Constraint(
        kind=ConstraintKind.COMMAND_REGEX,
        pattern=r"rm\s+-rf",
        source_entry_id="e1",
        verbatim="no rm",
    )
    free = Constraint(
        kind=ConstraintKind.FREE_TEXT,
        pattern="be careful",
        source_entry_id="e1",
        verbatim="be careful",
    )
    assert constraint_matches(tool_c, tool_name="write_file", args={"path": "x"})
    assert not constraint_matches(tool_c, tool_name="read_file", args={"path": "x"})
    assert constraint_matches(cmd_c, tool_name="run_command", args={"command": "rm -rf /tmp/x"})
    assert not constraint_matches(free, tool_name="write_file", args={"path": "be careful"})
    assert glob_match("db/migrations/001.sql", "db/migrations/**")


def test_cold_state_first_write_is_not_write_without_read() -> None:
    mailbox = VerdictMailbox()
    tracker = ProgressTracker(
        mailbox,
        ledger_fn=lambda: None,
        config=VerifierTrackerConfig(enabled=True, min_turn_before_verdict=1),
    )
    tracker._observe_tool(
        _payload(
            event=HookEvent.POST_TOOL,
            tool_name="write_file",
            tool_args={"path": "new.md"},
            inner_turn=3,
        )
    )
    assert mailbox.take_ready("t1") == []


def test_second_unread_write_emits_write_without_read() -> None:
    mailbox = VerdictMailbox()
    tracker = ProgressTracker(
        mailbox,
        ledger_fn=lambda: None,
        config=VerifierTrackerConfig(enabled=True, min_turn_before_verdict=1),
    )
    tracker._observe_tool(
        _payload(
            event=HookEvent.POST_TOOL,
            tool_name="read_file",
            tool_args={"path": "a.md"},
            inner_turn=3,
        )
    )
    tracker._observe_tool(
        _payload(
            event=HookEvent.POST_TOOL,
            tool_name="write_file",
            tool_args={"path": "b.md"},
            inner_turn=3,
        )
    )
    verdicts = mailbox.take_ready("t1")
    assert len(verdicts) == 1
    assert "write_without_read" in verdicts[0].triggering_signals
    assert verdicts[0].severity == "none"


def test_min_turn_suppresses_non_ledger_signals() -> None:
    mailbox = VerdictMailbox()
    tracker = ProgressTracker(
        mailbox,
        ledger_fn=lambda: None,
        config=VerifierTrackerConfig(enabled=True, min_turn_before_verdict=3),
    )
    tracker._observe_tool(
        _payload(
            event=HookEvent.POST_TOOL,
            tool_name="read_file",
            tool_args={"path": "a.md"},
            inner_turn=1,
        )
    )
    tracker._observe_tool(
        _payload(
            event=HookEvent.POST_TOOL,
            tool_name="write_file",
            tool_args={"path": "b.md"},
            inner_turn=1,
        )
    )
    assert mailbox.take_ready("t1") == []


def test_error_streak_uses_provider_inner_turn_when_tool_omits_it() -> None:
    mailbox = VerdictMailbox()
    tracker = ProgressTracker(
        mailbox,
        ledger_fn=lambda: None,
        config=VerifierTrackerConfig(enabled=True, min_turn_before_verdict=3),
    )
    tracker._observe_provider(
        _payload(
            event=HookEvent.AFTER_PROVIDER_RESPONSE,
            inner_turn=3,
            assistant_text="",
            tool_requests=[{"name": "read_file"}],
        )
    )
    for _ in range(3):
        tracker._observe_tool(
            _payload(
                event=HookEvent.POST_TOOL,
                tool_name="read_file",
                tool_args={"path": "x"},
                tool_error="boom",
                inner_turn=None,
            )
        )
    verdicts = mailbox.take_ready("t1")
    assert any("error_streak" in v.triggering_signals for v in verdicts)


def test_error_streak_emits_after_three() -> None:
    mailbox = VerdictMailbox()
    tracker = ProgressTracker(
        mailbox,
        ledger_fn=lambda: None,
        config=VerifierTrackerConfig(enabled=True, min_turn_before_verdict=1),
    )
    for _ in range(3):
        tracker._observe_tool(
            _payload(
                event=HookEvent.POST_TOOL,
                tool_name="read_file",
                tool_args={"path": "x"},
                tool_error="boom",
                inner_turn=3,
            )
        )
    verdicts = mailbox.take_ready("t1")
    assert any("error_streak" in v.triggering_signals for v in verdicts)


def test_budget_burn_and_no_progress_are_logged_not_emitted() -> None:
    mailbox = VerdictMailbox()
    tracker = ProgressTracker(
        mailbox,
        ledger_fn=lambda: None,
        config=VerifierTrackerConfig(enabled=True, min_turn_before_verdict=1),
    )
    for _ in range(4):
        tracker._observe_provider(
            _payload(
                event=HookEvent.AFTER_PROVIDER_RESPONSE,
                inner_turn=4,
                assistant_text="",
                tool_requests=[{"name": "run_command"}],
                usage={"input_tokens": 120_000, "output_tokens": 10},
            )
        )
    assert mailbox.take_ready("t1") == []


@pytest.mark.asyncio
async def test_free_text_never_fires_constraint_touch() -> None:
    mailbox = VerdictMailbox()
    store = InMemoryGoalLedgerStore()
    ledger = GoalLedger(
        store,
        ScriptedClassifier(
            [
                Classification(
                    intent=Intent.NEW_GOAL,
                    relates_to=None,
                    constraints=(
                        ConstraintDraft(
                            kind=ConstraintKind.FREE_TEXT,
                            pattern="don't be silly",
                            verbatim="don't be silly",
                        ),
                    ),
                )
            ]
        ),
    )
    ledger.admit("t1", "don't be silly", provenance=Provenance.HUMAN, channel=Channel.MESSAGE)
    await ledger.wait_idle("t1")
    tracker = ProgressTracker(
        mailbox,
        ledger_fn=lambda: ledger,
        config=VerifierTrackerConfig(enabled=True, min_turn_before_verdict=1),
    )
    tracker._observe_tool(
        _payload(
            event=HookEvent.POST_TOOL,
            tool_name="write_file",
            tool_args={"path": "notes.md"},
            inner_turn=3,
        )
    )
    assert mailbox.take_ready("t1") == []
    ledger.close()


@pytest.mark.asyncio
async def test_path_constraint_touch_from_ledger() -> None:
    mailbox = VerdictMailbox()
    store = InMemoryGoalLedgerStore()
    ledger = GoalLedger(
        store,
        ScriptedClassifier(
            [
                Classification(
                    intent=Intent.NEW_GOAL,
                    relates_to=None,
                    constraints=(
                        ConstraintDraft(
                            kind=ConstraintKind.PATH_GLOB,
                            pattern="db/migrations/**",
                            verbatim="don't touch migrations",
                        ),
                    ),
                )
            ]
        ),
    )
    ledger.admit(
        "t1", "don't touch migrations", provenance=Provenance.HUMAN, channel=Channel.MESSAGE
    )
    await ledger.wait_idle("t1")
    tracker = ProgressTracker(
        mailbox,
        ledger_fn=lambda: ledger,
        config=VerifierTrackerConfig(enabled=True, min_turn_before_verdict=3),
    )
    tracker._observe_tool(
        _payload(
            event=HookEvent.POST_TOOL,
            tool_name="write_file",
            tool_args={"path": "db/migrations/001.sql"},
            inner_turn=1,
        )
    )
    verdicts = mailbox.take_ready("t1")
    assert len(verdicts) == 1
    assert "constraint_touch" in verdicts[0].triggering_signals
    assert verdicts[0].severity == "none"
    ledger.close()


@pytest.mark.asyncio
async def test_run_yields_queued_verifier_verdict_before_turn_complete() -> None:
    from monkeybot.core.llm.provider import Done, TextDelta, UsageEvent

    mailbox = VerdictMailbox()
    mailbox.put(
        "t1",
        VerifierVerdict(
            request_id="r1",
            verdict_id="v1",
            checkpoint_id="r1:1",
            status="drifting",
            severity="none",
            confidence=0.9,
            rationale="constraint_touch",
            triggering_signals=("constraint_touch",),
        ),
    )
    ctx = replace(loop_ctx(), verdict_mailbox=mailbox)
    hist = FakeHistory()
    events = []
    async for event in run(
        "hello",
        ctx,
        provider=FakeProvider(
            [[TextDelta(text="hi"), UsageEvent(input_tokens=1, output_tokens=1), Done()]]
        ),
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        events.append(event)
    kinds = [type(e) for e in events]
    assert VerifierVerdict in kinds
    assert kinds.index(VerifierVerdict) < kinds.index(TurnComplete)
    system_rows = [m for m in hist.rows if m.role == "system"]
    assert len(system_rows) == 1
    from monkeybot.core.types.content_blocks import SystemNotification

    assert isinstance(system_rows[0].content[0], SystemNotification)
    assert system_rows[0].content[0].notification_type == "verifierVerdict"


@pytest.mark.asyncio
async def test_tail_grace_is_skipped_when_no_judge_call_is_pending() -> None:
    import time

    from monkeybot.core.runtime.turn_loop import _take_ready

    mailbox = VerdictMailbox()
    started = time.monotonic()
    assert await _take_ready(mailbox, "t1", "r1", grace_s=1.0) == []
    assert time.monotonic() - started < 0.2

    mailbox.mark_pending("t1", "r1")
    started = time.monotonic()
    assert await _take_ready(mailbox, "t1", "r1", grace_s=0.2) == []
    assert time.monotonic() - started >= 0.2


@pytest.mark.asyncio
async def test_run_commits_pending_judge_after_tail_grace() -> None:
    from monkeybot.core.config.settings import VerifierConfig, VerifierJudgeConfig
    from monkeybot.core.hooks import HookEvent, HookManager

    class _Cfg:
        env_values: dict[str, str] = {}
        verifier = VerifierConfig(judge=VerifierJudgeConfig(tail_grace_s=0.4))

    mailbox = VerdictMailbox()
    mgr = HookManager()

    async def _late_judge(payload: HookPayload) -> None:
        mailbox.mark_pending(payload.thread_id, payload.request_id)

        async def _deposit() -> None:
            import asyncio

            await asyncio.sleep(0.12)
            mailbox.put(
                payload.thread_id,
                VerifierVerdict(
                    request_id=payload.request_id,
                    verdict_id="v-late",
                    checkpoint_id="r1:1",
                    status="drifting",
                    severity="nudge",
                    rationale="error_streak",
                    triggering_signals=("error_streak",),
                    correction="[Verifier] stop retrying the missing file",
                ),
            )
            mailbox.clear_pending(payload.thread_id, payload.request_id)

        import asyncio

        asyncio.create_task(_deposit())

    mgr.register(HookEvent.POST_TOOL, _late_judge)
    ctx = replace(loop_ctx(), verdict_mailbox=mailbox, config=_Cfg())  # type: ignore[arg-type]
    from monkeybot.core.llm.provider import Done, TextDelta, ToolCall, UsageEvent

    events = []
    async for event in run(
        "hello",
        ctx,
        provider=FakeProvider(
            [
                [
                    ToolCall(call_id="c1", name="run_command", args={"command": "echo hi"}),
                    UsageEvent(input_tokens=1, output_tokens=1),
                    Done(),
                ],
                [TextDelta(text="done"), UsageEvent(input_tokens=1, output_tokens=1), Done()],
            ]
        ),
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
        hook_manager=mgr,
    ):
        events.append(event)
    assert any(isinstance(e, VerifierVerdict) and e.verdict_id == "v-late" for e in events)


def test_cap_severity() -> None:
    from monkeybot.core.verifier.severity import cap_severity

    assert cap_severity("block", "nudge") == "nudge"
    assert cap_severity("nudge", "none") == "none"
    assert cap_severity("none", "nudge") == "none"
    assert cap_severity("replan", "replan") == "replan"


@pytest.mark.asyncio
async def test_nudge_reaches_provider_while_active() -> None:
    from monkeybot.core.hooks import HookManager
    from monkeybot.core.llm.provider import Done, TextDelta, ToolCall, UsageEvent
    from monkeybot.core.types.content_blocks import Text
    from monkeybot.core.verifier.actuator import NudgeActuator

    mailbox = VerdictMailbox()
    mailbox.put(
        "t1",
        VerifierVerdict(
            request_id="r1",
            verdict_id="v1",
            checkpoint_id="r1:1",
            status="drifting",
            severity="nudge",
            rationale="constraint_touch",
            triggering_signals=("constraint_touch",),
            correction="[Verifier] leave the migrations alone",
        ),
    )
    mgr = HookManager()
    NudgeActuator(mailbox).register(mgr)
    ctx = replace(loop_ctx(), verdict_mailbox=mailbox)
    prov = FakeProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={"command": "echo hi"}),
                UsageEvent(input_tokens=1, output_tokens=1),
                Done(),
            ],
            [TextDelta(text="ok"), UsageEvent(input_tokens=1, output_tokens=1), Done()],
        ]
    )
    events = []
    async for event in run(
        "hello",
        ctx,
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
        hook_manager=mgr,
    ):
        events.append(event)
    assert any(isinstance(e, VerifierVerdict) for e in events)
    assert len(prov.stream_messages) >= 2
    for msgs in prov.stream_messages:
        joined = " ".join(b.text for msg in msgs for b in msg.content if isinstance(b, Text))
        assert "Stay inside the user's stated constraints" in joined
        assert "## Verifier" in joined
    snaps = [e for e in events if isinstance(e, SystemPromptSnapshot)]
    assert any(
        "## Verifier" in e.text and "Stay inside the user's stated constraints" in e.text
        for e in snaps
    )


@pytest.mark.asyncio
async def test_nudge_clears_after_successful_tool() -> None:
    from monkeybot.core.hooks import HookManager
    from monkeybot.core.llm.provider import Done, TextDelta, ToolCall, UsageEvent
    from monkeybot.core.types.content_blocks import Text
    from monkeybot.core.verifier.actuator import NudgeActuator

    mailbox = VerdictMailbox()
    mailbox.put(
        "t1",
        VerifierVerdict(
            request_id="r1",
            verdict_id="v1",
            checkpoint_id="r1:1",
            status="stuck",
            severity="nudge",
            rationale="error_streak",
            triggering_signals=("error_streak",),
            correction="Stop retrying the same failing action",
        ),
    )
    mgr = HookManager()
    NudgeActuator(mailbox).register(mgr)
    ProgressTracker(
        mailbox,
        ledger_fn=lambda: None,
        config=VerifierTrackerConfig(
            enabled=True, min_turn_before_verdict=0, suspicion_threshold=1
        ),
    ).register(mgr)
    ctx = replace(loop_ctx(), verdict_mailbox=mailbox)
    prov = FakeProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={"command": "echo hi"}),
                UsageEvent(input_tokens=1, output_tokens=1),
                Done(),
            ],
            [TextDelta(text="ok"), UsageEvent(input_tokens=1, output_tokens=1), Done()],
        ]
    )
    async for _ in run(
        "hello",
        ctx,
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
        hook_manager=mgr,
    ):
        pass
    first = " ".join(
        b.text for msg in prov.stream_messages[0] for b in msg.content if isinstance(b, Text)
    )
    second = " ".join(
        b.text for msg in prov.stream_messages[1] for b in msg.content if isinstance(b, Text)
    )
    assert "Stop retrying" in first
    assert "Stop retrying" not in second


@pytest.mark.asyncio
async def test_stale_verdict_does_not_activate_after_recovery() -> None:
    mailbox = VerdictMailbox()
    mailbox.set_current_signals("t1", "r1", [])
    verdict = VerifierVerdict(
        request_id="r1",
        verdict_id="v-stale",
        checkpoint_id="r1:3",
        status="stuck",
        severity="nudge",
        rationale="error_streak",
        triggering_signals=("error_streak",),
        correction="Stop retrying",
    )
    assert mailbox.activate_nudge("t1", "r1", verdict, "Stop retrying") is False
    assert mailbox.peek_nudge("t1", "r1") is None


def test_active_nudge_merges_overlapping_signals() -> None:
    mailbox = VerdictMailbox()
    first = VerifierVerdict(
        request_id="r1",
        verdict_id="v1",
        checkpoint_id="r1:3",
        status="stuck",
        severity="nudge",
        triggering_signals=("error_streak",),
        correction="first",
    )
    second = VerifierVerdict(
        request_id="r1",
        verdict_id="v2",
        checkpoint_id="r1:5",
        status="stuck",
        severity="nudge",
        triggering_signals=("error_streak", "write_without_read"),
        correction="second",
    )
    assert mailbox.activate_nudge("t1", "r1", first)
    assert mailbox.activate_nudge("t1", "r1", second) is True
    note = mailbox.peek_nudge("t1", "r1")
    assert note is not None
    assert "Stop retrying the same failing action" in note
    assert "Read the file first" in note
    mailbox.set_current_signals("t1", "r1", ["write_without_read"])
    assert mailbox.peek_nudge("t1", "r1") is not None
    mailbox.set_current_signals("t1", "r1", [])
    assert mailbox.peek_nudge("t1", "r1") is None


def test_nudge_does_not_leak_across_requests() -> None:
    mailbox = VerdictMailbox()
    verdict = VerifierVerdict(
        request_id="r1",
        verdict_id="v1",
        checkpoint_id="r1:1",
        status="stuck",
        severity="nudge",
        triggering_signals=("error_streak",),
        correction="stay",
    )
    assert mailbox.activate_nudge("t1", "r1", verdict)
    mailbox.clear_request("t1", "r1")
    assert mailbox.peek_nudge("t1", "r1") is None
    assert mailbox.peek_nudge("t1", "r2") is None


@pytest.mark.asyncio
async def test_replan_empties_tools_for_exactly_one_turn() -> None:
    from monkeybot.core.config.settings import VerifierConfig, VerifierEscalationConfig
    from monkeybot.core.llm.provider import Done, TextDelta, ToolCall, UsageEvent
    from monkeybot.core.types.content_blocks import Text
    from tests.core.test_doom_loop import ToolsRecordingProvider

    class _Cfg:
        env_values: dict[str, str] = {}
        verifier = VerifierConfig(
            escalation=VerifierEscalationConfig(max_severity="replan"),
        )

    mailbox = VerdictMailbox()
    mailbox.put(
        "t1",
        VerifierVerdict(
            request_id="r1",
            verdict_id="v1",
            checkpoint_id="r1:1",
            status="drifting",
            severity="replan",
            rationale="constraint_touch",
            triggering_signals=("constraint_touch",),
            correction="[Verifier] leave the migrations alone",
        ),
    )
    ctx = replace(loop_ctx(), verdict_mailbox=mailbox, config=_Cfg())  # type: ignore[arg-type]
    prov = ToolsRecordingProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={"command": "echo hi"}),
                UsageEvent(input_tokens=1, output_tokens=1),
                Done(),
            ],
            [TextDelta(text="ok"), UsageEvent(input_tokens=1, output_tokens=1), Done()],
        ]
    )
    events = []
    async for event in run(
        "hello",
        ctx,
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        events.append(event)
    assert any(isinstance(e, VerifierVerdict) for e in events)
    assert prov.stream_tools[0] == []
    assert prov.stream_tools[1] == ["run_command"]
    first = " ".join(
        b.text for msg in prov.stream_messages[0] for b in msg.content if isinstance(b, Text)
    )
    assert "Stay inside the user's stated constraints" in first
    assert "Do not call tools this turn" in first


@pytest.mark.asyncio
async def test_replan_from_a_finished_request_does_not_leak_into_the_next() -> None:
    """A note stashed by a tail drain has no inner turn left in its own request;
    it must be dropped, not applied to the next user message."""
    from monkeybot.core.config.settings import VerifierConfig, VerifierEscalationConfig
    from monkeybot.core.llm.provider import Done, TextDelta, ToolCall, UsageEvent
    from monkeybot.core.types.content_blocks import Text
    from tests.core.test_doom_loop import ToolsRecordingProvider

    class _Cfg:
        env_values: dict[str, str] = {}
        verifier = VerifierConfig(
            escalation=VerifierEscalationConfig(max_severity="replan"),
        )

    mailbox = VerdictMailbox()
    mailbox.put_replan(
        "t1",
        "r1",
        "[Verifier] leave the migrations alone\nDo not call tools this turn.",
    )
    ctx = replace(
        loop_ctx(),
        request_id="r2",
        verdict_mailbox=mailbox,
        config=_Cfg(),  # type: ignore[arg-type]
    )
    prov = ToolsRecordingProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={"command": "date"}),
                UsageEvent(input_tokens=1, output_tokens=1),
                Done(),
            ],
            [TextDelta(text="ok"), UsageEvent(input_tokens=1, output_tokens=1), Done()],
        ]
    )
    async for _ in run(
        "what is the weather",
        ctx,
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        pass
    assert prov.stream_tools[0] == ["run_command"]
    first = " ".join(
        b.text for msg in prov.stream_messages[0] for b in msg.content if isinstance(b, Text)
    )
    assert "leave the migrations alone" not in first


@pytest.mark.asyncio
async def test_block_denies_mutating_tool_and_allows_read_only() -> None:
    from monkeybot.core.config.settings import VerifierConfig, VerifierEscalationConfig
    from monkeybot.core.llm.provider import Done, TextDelta, ToolCall, UsageEvent
    from monkeybot.core.verifier.inspector import VerifierInspector

    class _Cfg:
        env_values: dict[str, str] = {}
        verifier = VerifierConfig(
            escalation=VerifierEscalationConfig(max_severity="block"),
        )

    mailbox = VerdictMailbox()
    mailbox.put(
        "t1",
        VerifierVerdict(
            request_id="r1",
            verdict_id="v1",
            checkpoint_id="r1:1",
            status="drifting",
            severity="block",
            rationale="constraint_touch",
            triggering_signals=("constraint_touch",),
            correction="[Verifier] leave the migrations alone",
        ),
    )
    ctx = replace(
        loop_ctx(),
        verdict_mailbox=mailbox,
        config=_Cfg(),  # type: ignore[arg-type]
        tools=[
            ToolDef("run_command", "Run shell", {}),
            ToolDef("read_file", "Read", {}, parallel_safe=True, read_only=True),
        ],
    )
    exe = RecordingExecutor()
    prov = FakeProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={"command": "echo hi"}),
                UsageEvent(input_tokens=1, output_tokens=1),
                Done(),
            ],
            [
                ToolCall(call_id="c2", name="read_file", args={"path": "README.md"}),
                UsageEvent(input_tokens=1, output_tokens=1),
                Done(),
            ],
            [TextDelta(text="ok"), UsageEvent(input_tokens=1, output_tokens=1), Done()],
        ]
    )
    events = []
    async for event in run(
        "hello",
        ctx,
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector(), VerifierInspector(mailbox)],
        tool_executor=exe,
        max_turns=4,
    ):
        events.append(event)
    assert exe.calls and exe.calls[0].name == "read_file"
    assert all(c.name != "run_command" for c in exe.calls)
    assert any(
        isinstance(e, ToolCallResult)
        and e.tool == "run_command"
        and isinstance(e.error, str)
        and "Stay inside the user's stated constraints" in e.error
        for e in events
    )


@pytest.mark.asyncio
async def test_run_yields_queued_verdict_before_error() -> None:
    class BoomProvider(FakeProvider):
        async def stream(self, *args: object, **kwargs: object):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    mailbox = VerdictMailbox()
    mailbox.put(
        "t1",
        VerifierVerdict(
            request_id="r1",
            verdict_id="v1",
            checkpoint_id="r1:1",
            status="drifting",
            severity="none",
            confidence=0.9,
            rationale="constraint_touch",
            triggering_signals=("constraint_touch",),
        ),
    )
    ctx = replace(loop_ctx(), verdict_mailbox=mailbox)
    events = []
    async for event in run(
        "hello",
        ctx,
        provider=BoomProvider([[]]),
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        events.append(event)
    kinds = [type(e) for e in events]
    assert kinds.index(VerifierVerdict) < kinds.index(Error)
    assert kinds.index(Error) < kinds.index(TurnComplete)


def test_mailbox_caps_per_thread_and_evicts_idle_threads() -> None:
    from monkeybot.core.verifier.mailbox import _PER_THREAD_MAX, _THREAD_CAP

    mailbox = VerdictMailbox()

    def _verdict(i: int) -> VerifierVerdict:
        return VerifierVerdict(
            request_id="r",
            verdict_id=str(i),
            checkpoint_id=f"r:{i}",
            status="drifting",
            severity="none",
        )

    for i in range(_PER_THREAD_MAX + 5):
        mailbox.put("t1", _verdict(i))
    ready = mailbox.take_ready("t1")
    assert len(ready) == _PER_THREAD_MAX
    assert ready[0].verdict_id == "5"

    for i in range(_THREAD_CAP + 1):
        mailbox.put(f"t{i}", _verdict(i))
    assert mailbox.take_ready("t0") == []
    assert len(mailbox.take_ready(f"t{_THREAD_CAP}")) == 1


def test_mailbox_sticky_nudge_and_last_caps_after_drain() -> None:
    from monkeybot.core.verifier.mailbox import _THREAD_CAP

    mailbox = VerdictMailbox()
    first = VerifierVerdict(
        request_id="r1",
        verdict_id="v1",
        checkpoint_id="r1:1",
        status="stuck",
        severity="nudge",
        triggering_signals=("error_streak",),
        correction="first",
    )
    mailbox.activate_nudge("t1", "r1", first)
    mailbox.activate_nudge(
        "t1",
        "r1",
        VerifierVerdict(
            request_id="r1",
            verdict_id="v2",
            checkpoint_id="r1:2",
            status="stuck",
            severity="nudge",
            triggering_signals=("error_streak",),
            correction="second",
        ),
    )
    note = mailbox.peek_nudge("t1", "r1")
    assert note is not None
    assert "Stop retrying the same failing action" in note
    assert mailbox.peek_nudge("t1", "r2") is None
    mailbox.clear_request("t1", "r1")
    assert mailbox.peek_nudge("t1", "r1") is None
    mailbox.put_replan("t1", "r1", "stale")
    assert mailbox.take_replan("t1", "r2") is None

    def _verdict(i: int) -> VerifierVerdict:
        return VerifierVerdict(
            request_id="r",
            verdict_id=str(i),
            checkpoint_id=f"r:{i}",
            status="drifting",
            severity="none",
        )

    for i in range(_THREAD_CAP + 1):
        mailbox.put(f"t{i}", _verdict(i))
        mailbox.take_ready(f"t{i}")
    assert mailbox.last("t0") is None
    assert mailbox.last(f"t{_THREAD_CAP}") is not None


def test_done_when_requires_path_boundary() -> None:
    from monkeybot.core.verifier.tracker import _done_when_satisfied

    written = {"other/docs/a.md.bak", "notes.md"}
    assert not _done_when_satisfied("docs/a.md", written)
    assert _done_when_satisfied("docs/a.md", {"docs/a.md"})
    assert _done_when_satisfied("docs/a.md", {"src/docs/a.md"})
    assert _done_when_satisfied("notes.md", written)


def _scoped_verdict(verdict_id: str, request_id: str) -> VerifierVerdict:
    return VerifierVerdict(
        request_id=request_id,
        verdict_id=verdict_id,
        checkpoint_id=f"{request_id}:1",
        status="drifting",
        severity="nudge",
        triggering_signals=("error_streak",),
        correction="ignore this free-form text",
    )


def test_ready_and_pending_are_request_scoped() -> None:
    mailbox = VerdictMailbox()
    mailbox.mark_pending("t1", "r1")
    mailbox.put("t1", _scoped_verdict("v1", "r1"))
    mailbox.put("t1", _scoped_verdict("v2", "r2"))
    assert mailbox.pending("t1", "r1") is True
    assert mailbox.pending("t1", "r2") is False
    assert [v.verdict_id for v in mailbox.take_ready("t1", "r1")] == ["v1"]
    assert [v.verdict_id for v in mailbox.take_ready("t1", "r2")] == ["v2"]


def test_put_after_clear_request_is_dropped() -> None:
    mailbox = VerdictMailbox()
    mailbox.clear_request("t1", "r1")
    assert mailbox.put("t1", _scoped_verdict("late", "r1")) is False
    assert mailbox.take_ready("t1", "r1") == []
    assert mailbox.put("t1", _scoped_verdict("next", "r2")) is True
    assert [v.verdict_id for v in mailbox.take_ready("t1", "r2")] == ["next"]


@pytest.mark.asyncio
async def test_take_ready_collects_all_ready_verdicts_for_the_request() -> None:
    from monkeybot.core.runtime.turn_loop import _take_ready

    mailbox = VerdictMailbox()
    mailbox.mark_pending("t1", "r1")
    mailbox.mark_pending("t1", "r1")
    mailbox.put("t1", _scoped_verdict("v1", "r1"))
    mailbox.put("t1", _scoped_verdict("v2", "r1"))
    mailbox.put("t1", _scoped_verdict("other", "r2"))
    mailbox.clear_pending("t1", "r1")
    mailbox.clear_pending("t1", "r1")
    ready = await _take_ready(mailbox, "t1", "r1", grace_s=1.0)
    assert [v.verdict_id for v in ready] == ["v1", "v2"]
    assert [v.verdict_id for v in mailbox.take_ready("t1", "r2")] == ["other"]


@pytest.mark.asyncio
async def test_tail_grace_exits_when_request_pending_reaches_zero() -> None:
    import asyncio
    import time

    from monkeybot.core.runtime.turn_loop import _take_ready

    mailbox = VerdictMailbox()
    mailbox.mark_pending("t1", "r1")

    async def _settle() -> None:
        await asyncio.sleep(0.04)
        mailbox.clear_pending("t1", "r1")

    asyncio.create_task(_settle())
    started = time.monotonic()
    assert await _take_ready(mailbox, "t1", "r1", grace_s=2.0) == []
    assert time.monotonic() - started < 0.4
