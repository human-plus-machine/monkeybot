"""Empty-completion guard: no text and no tools → bounded recovery."""

from __future__ import annotations

import pytest

from monkeybot.core.llm.provider import Done, TextDelta, ThinkingDelta
from monkeybot.core.runtime.events import Error, TurnComplete
from monkeybot.core.runtime.loop import (
    _EMPTY_COMPLETION_ERROR,
    _EMPTY_COMPLETION_EXHAUSTED_ERROR,
    _EMPTY_COMPLETION_RECOVERY_NOTE,
    run,
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
    errors = [e for e in events if isinstance(e, Error)]
    assert len(errors) == 1
    assert errors[0].error == _EMPTY_COMPLETION_ERROR

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
    error_texts = [e.error for e in events if isinstance(e, Error)]
    assert error_texts.count(_EMPTY_COMPLETION_ERROR) == 2
    assert _EMPTY_COMPLETION_EXHAUSTED_ERROR in error_texts


@pytest.mark.asyncio
async def test_run_empty_after_tools_emits_error_and_recovers() -> None:
    """Post-tool empty turns must surface Error (not silent continue) then recover."""
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
    empty_errors = [
        e for e in events if isinstance(e, Error) and e.error == _EMPTY_COMPLETION_ERROR
    ]
    assert len(empty_errors) == 1
    followup_system = "".join(
        b.text
        for m in prov.stream_messages[2]
        if m.role == "system"
        for b in m.content
        if hasattr(b, "text")
    )
    assert _EMPTY_COMPLETION_RECOVERY_NOTE in followup_system
