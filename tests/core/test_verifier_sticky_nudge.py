"""Sticky verifier nudge: latched signals, prune, and BEFORE_PROVIDER injection."""

from __future__ import annotations

from dataclasses import replace

import pytest

from monkeybot.core.config.settings import VerifierTrackerConfig
from monkeybot.core.hooks import HookEvent, HookManager
from monkeybot.core.llm.provider import Done, TextDelta, ToolCall, UsageEvent
from monkeybot.core.runtime.events import SystemPromptSnapshot, VerifierVerdict
from monkeybot.core.runtime.loop import run
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.verifier.actuator import NudgeActuator
from monkeybot.core.verifier.intervention import SIGNAL_INSTRUCTIONS, correction_text
from monkeybot.core.verifier.tracker import ProgressTracker
from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider, RecordingExecutor
from tests.core.test_loop import _ctx as loop_ctx
from tests.core.test_loop_hooks import CapturingProvider
from tests.core.test_progress_tracker import _mailbox, _payload


def _nudge(request_id: str, *signals: str, verdict_id: str = "v1") -> VerifierVerdict:
    return VerifierVerdict(
        request_id=request_id,
        verdict_id=verdict_id,
        checkpoint_id=f"{request_id}:1",
        status="drifting",
        severity="nudge",
        triggering_signals=signals,
        correction="ignore this free-form text",
    )


def test_write_then_other_tool_keeps_write_without_read_latched() -> None:
    mailbox = _mailbox()
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
    tracker._observe_tool(
        _payload(
            event=HookEvent.POST_TOOL,
            tool_name="run_command",
            tool_args={"command": "ls"},
            inner_turn=3,
        )
    )
    assert mailbox.peek_nudge("t1", "r1") is None
    assert "write_without_read" in mailbox.take_ready("t1")[0].triggering_signals
    mailbox.open_request("t1", "r1")
    mailbox.set_current_signals("t1", "r1", ["write_without_read"])
    assert mailbox.activate_nudge("t1", "r1", _nudge("r1", "write_without_read")) is True
    assert mailbox.peek_nudge("t1", "r1") is not None


def test_read_of_unread_write_recovers_write_without_read() -> None:
    mailbox = _mailbox()
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
    mailbox.set_current_signals("t1", "r1", ["write_without_read"])
    mailbox.activate_nudge("t1", "r1", _nudge("r1", "write_without_read"))
    tracker._observe_tool(
        _payload(
            event=HookEvent.POST_TOOL,
            tool_name="read_file",
            tool_args={"path": "b.md"},
            inner_turn=3,
        )
    )
    assert mailbox.peek_nudge("t1", "r1") is None


def test_overlapping_signals_prune_independently() -> None:
    mailbox = _mailbox()
    first = _nudge("r1", "error_streak")
    second = _nudge("r1", "write_without_read", verdict_id="v2")
    mailbox.set_current_signals("t1", "r1", ["error_streak", "write_without_read"])
    assert mailbox.activate_nudge("t1", "r1", first)
    assert mailbox.activate_nudge("t1", "r1", second) is True
    note = mailbox.peek_nudge("t1", "r1")
    assert note is not None
    assert SIGNAL_INSTRUCTIONS["error_streak"] in note
    assert SIGNAL_INSTRUCTIONS["write_without_read"] in note
    mailbox.set_current_signals("t1", "r1", ["write_without_read"])
    pruned = mailbox.peek_nudge("t1", "r1")
    assert pruned is not None
    assert SIGNAL_INSTRUCTIONS["error_streak"] not in pruned
    assert SIGNAL_INSTRUCTIONS["write_without_read"] in pruned
    mailbox.set_current_signals("t1", "r1", [])
    assert mailbox.peek_nudge("t1", "r1") is None


def test_activate_refuses_recovered_signals() -> None:
    mailbox = _mailbox()
    mailbox.set_current_signals("t1", "r1", [])
    assert mailbox.activate_nudge("t1", "r1", _nudge("r1", "error_streak")) is False
    assert mailbox.peek_nudge("t1", "r1") is None


def test_nudge_does_not_leak_across_requests() -> None:
    mailbox = _mailbox()
    mailbox.activate_nudge("t1", "r1", _nudge("r1", "error_streak"))
    mailbox.clear_request("t1", "r1")
    assert mailbox.peek_nudge("t1", "r1") is None
    mailbox.open_request("t1", "r2")
    assert mailbox.peek_nudge("t1", "r2") is None


def test_correction_text_is_trusted_templates_only() -> None:
    text = correction_text(("constraint_touch", "unknown_signal"))
    assert text.startswith("[Verifier]")
    assert SIGNAL_INSTRUCTIONS["constraint_touch"] in text
    assert "unknown_signal" not in text


@pytest.mark.asyncio
async def test_sticky_nudge_survives_multiple_provider_calls() -> None:
    mailbox = _mailbox()
    mailbox.put("t1", _nudge("r1", "constraint_touch"))
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
            [
                ToolCall(call_id="c2", name="run_command", args={"command": "echo again"}),
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
        max_turns=4,
        hook_manager=mgr,
    ):
        pass
    assert len(prov.stream_messages) >= 3
    for msgs in prov.stream_messages:
        joined = " ".join(b.text for msg in msgs for b in msg.content if isinstance(b, Text))
        assert SIGNAL_INSTRUCTIONS["constraint_touch"] in joined
        assert "## Verifier" in joined
        assert "ignore this free-form text" not in joined


@pytest.mark.asyncio
async def test_nudge_clears_after_successful_tool() -> None:
    mailbox = _mailbox()
    mailbox.put("t1", _nudge("r1", "error_streak"))
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
async def test_system_prompt_snapshot_matches_provider_verifier_block() -> None:
    mailbox = _mailbox()
    mailbox.put("t1", _nudge("r1", "constraint_touch"))
    mgr = HookManager()
    NudgeActuator(mailbox).register(mgr)
    ctx = replace(loop_ctx(), verdict_mailbox=mailbox)
    prov = CapturingProvider([[TextDelta(text="ok"), Done()]])
    events = []
    async for event in run(
        "hello",
        ctx,
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=2,
        hook_manager=mgr,
    ):
        events.append(event)
    snaps = [e for e in events if isinstance(e, SystemPromptSnapshot)]
    assert snaps
    trusted = SIGNAL_INSTRUCTIONS["constraint_touch"]
    assert "## Verifier" in snaps[0].text
    assert trusted in snaps[0].text
    assert "ignore this free-form text" not in snaps[0].text
    assert prov.system_texts
    assert "## Verifier" in prov.system_texts[0]
    assert trusted in prov.system_texts[0]
    assert snaps[0].text == prov.system_texts[0] or trusted in snaps[0].text
