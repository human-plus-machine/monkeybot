"""Empty-completion guard: no text and no tools → bounded recovery."""

from __future__ import annotations

import pytest

from monkeybot.core.llm.provider import Done, TextDelta, ThinkingDelta
from monkeybot.core.runtime.events import Error, TurnComplete
from monkeybot.core.runtime.loop import run
from monkeybot.core.runtime.turn_loop import (
    _EMPTY_COMPLETION_EXHAUSTED_ERROR,
    _EMPTY_COMPLETION_RECOVERY_NOTE,
)
from monkeybot.core.tools.types import ToolExecutionResult
from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider, RecordingExecutor, _ctx


@pytest.mark.asyncio
async def test_run_retries_thinking_only_empty_completion() -> None:
    """Thinking-only first turn must recover with a harness note, then answer."""
    prov = FakeProvider(
        [
            [
                ThinkingDelta(text="I should call a tool but will not."),
                Done(),
            ],
            [TextDelta(text="Here is a real answer."), Done()],
        ]
    )
    hist = FakeHistory()
    events = []
    async for e in run(
        "please do the thing",
        _ctx(),
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(ToolExecutionResult.ok_text("ok")),
        max_turns=5,
    ):
        events.append(e)

    assert prov.stream_calls == 2
    assert isinstance(events[-1], TurnComplete)
    # A successful recovery must not surface an Error: the user never sees a
    # failure for a retry the harness resolved on its own.
    errors = [e for e in events if isinstance(e, Error)]
    assert len(errors) == 0

    # Recovery note injected into the second provider call's system message.
    second_msgs = prov.stream_messages[1]
    system_text = "".join(
        b.text for m in second_msgs if m.role == "system" for b in m.content if hasattr(b, "text")
    )
    assert _EMPTY_COMPLETION_RECOVERY_NOTE in system_text
    assert any(
        getattr(b, "text", None) == "Here is a real answer."
        for m in hist.rows
        if m.role == "assistant"
        for b in m.content
    )


@pytest.mark.asyncio
async def test_run_empty_completion_exhausted_emits_error() -> None:
    """Two recovery attempts, then exhausted Error on the third empty turn."""
    prov = FakeProvider(
        [
            [ThinkingDelta(text="still thinking"), Done()],
            [Done()],
            [Done()],
        ]
    )
    events = []
    async for e in run(
        "hello",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(ToolExecutionResult.ok_text("ok")),
        max_turns=5,
    ):
        events.append(e)

    assert prov.stream_calls == 3
    assert isinstance(events[-1], TurnComplete)
    # Only the final, truly-exhausted attempt surfaces an Error; the two
    # recovery retries in between are silent (logged, not user-facing).
    error_texts = [e.error for e in events if isinstance(e, Error)]
    assert error_texts == [_EMPTY_COMPLETION_EXHAUSTED_ERROR]


@pytest.mark.asyncio
async def test_run_empty_after_tools_recovers_without_error() -> None:
    """Post-tool empty turns retry with a recovery note but stay silent (no Error) once recovered."""
    from monkeybot.core.llm.provider import ToolCall

    prov = FakeProvider(
        [
            [
                ToolCall(call_id="c1", name="run_command", args={"command": "echo hi"}),
                Done(),
            ],
            [ThinkingDelta(text="planning next step but emitting nothing"), Done()],
            [TextDelta(text="Done with the work."), Done()],
        ]
    )
    events = []
    async for e in run(
        "do it",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(ToolExecutionResult.ok_text("ok")),
        max_turns=6,
    ):
        events.append(e)

    assert prov.stream_calls == 3
    assert isinstance(events[-1], TurnComplete)
    assert not any(isinstance(e, Error) for e in events)
    followup_system = "".join(
        b.text
        for m in prov.stream_messages[2]
        if m.role == "system"
        for b in m.content
        if hasattr(b, "text")
    )
    assert _EMPTY_COMPLETION_RECOVERY_NOTE in followup_system
