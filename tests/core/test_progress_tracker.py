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
from monkeybot.core.runtime.events import TurnComplete, VerifierVerdict
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
    inner_turn: int = 3,
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
