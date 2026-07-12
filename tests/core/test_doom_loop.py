"""Doom-loop detection: consecutive identical tool calls (ok or error)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from monkeybot.core.llm.provider import Done, Message, ProviderEvent, TextDelta, ToolCall
from monkeybot.core.runtime.events import Error, TurnComplete
from monkeybot.core.runtime.loop import (
    _DoomLoopTracker,
    _doom_loop_texts,
    _effective_doom_loop_threshold,
    _tool_call_fingerprint,
    run,
)
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.types_tools import ToolDef
from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider, RecordingExecutor, _ctx


class ToolsRecordingProvider(FakeProvider):
    """FakeProvider that records tool names passed to each ``stream`` call."""

    def __init__(self, scripted: list[list[object]]) -> None:
        super().__init__(scripted)
        self.stream_tools: list[list[str]] = []

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        vertex_google_search: bool = False,
    ) -> AsyncIterator[ProviderEvent]:
        self.stream_tools.append([t.name for t in tools])
        async for ev in super().stream(
            messages,
            tools,
            model=model,
            thinking_budget=thinking_budget,
            vertex_google_search=vertex_google_search,
        ):
            yield ev


def _tool_call(call_id: str, *, args: dict[str, object] | None = None) -> ToolCall:
    return ToolCall(
        call_id=call_id,
        name="run_command",
        args=dict(args or {"command": "echo hi"}),
    )


def _failing_call(call_id: str) -> ToolCall:
    return _tool_call(call_id)


def test_tool_call_fingerprint_stable_under_key_order() -> None:
    a = _tool_call_fingerprint("run_command", {"b": 1, "a": 2})
    b = _tool_call_fingerprint("run_command", {"a": 2, "b": 1})
    assert a == b
    assert a != _tool_call_fingerprint("run_command", {"a": 3, "b": 1})


def test_doom_loop_tracker_triggers_on_identical_failures() -> None:
    tracker = _DoomLoopTracker(threshold=3)
    args = {"command": "echo hi"}
    tracker.record("run_command", args)
    tracker.record("run_command", args)
    assert tracker.take_error() is None
    tracker.record("run_command", args)
    error, note = _doom_loop_texts("run_command", 3)
    assert tracker.take_error() == error
    assert tracker.triggered is True
    assert tracker.force_no_tools is True
    assert tracker.recovery_note == note
    assert tracker.take_error() is None
    # While triggered (before consume_recovery), further records are no-ops.
    tracker.record("run_command", args)
    assert tracker.take_error() is None


def test_doom_loop_tracker_triggers_on_identical_successes() -> None:
    """Successful no-progress loops (e.g. screenshot spam) must trip the guard."""
    tracker = _DoomLoopTracker(threshold=3)
    args: dict[str, object] = {}
    tracker.record("browser__browser_screenshot", args)
    tracker.record("browser__browser_screenshot", args)
    assert tracker.take_error() is None
    tracker.record("browser__browser_screenshot", args)
    assert tracker.take_error() == _doom_loop_texts("browser__browser_screenshot", 3)[0]
    assert tracker.triggered is True
    assert tracker.force_no_tools is True


def test_doom_loop_tracker_success_does_not_reset_identical_streak() -> None:
    """Ok vs error does not reset — mixed outcomes with same args still count."""
    tracker = _DoomLoopTracker(threshold=3)
    args = {"command": "echo hi"}
    tracker.record("run_command", args)
    tracker.record("run_command", args)
    # Previously a success reset the streak; it must continue now.
    tracker.record("run_command", args)
    assert tracker.take_error() is not None


def test_doom_loop_tracker_rearms_after_recovery() -> None:
    """A second identical streak after recovery must trigger again."""
    tracker = _DoomLoopTracker(threshold=2)
    args = {"command": "echo hi"}
    tracker.record("run_command", args)
    tracker.record("run_command", args)
    assert tracker.take_error() is not None
    force, note = tracker.consume_recovery()
    assert force is True
    assert note is not None
    assert tracker.triggered is False
    assert tracker.streak_count == 0

    tracker.record("run_command", args)
    assert tracker.take_error() is None
    tracker.record("run_command", args)
    assert tracker.take_error() is not None
    assert tracker.force_no_tools is True
    assert tracker.consume_recovery()[0] is True


def test_doom_loop_tracker_consume_recovery() -> None:
    tracker = _DoomLoopTracker(threshold=1)
    tracker.record("run_command", {"command": "x"})
    assert tracker.take_error() is not None
    force, note = tracker.consume_recovery()
    assert force is True
    assert note is not None and "Doom loop detected" in note
    assert tracker.consume_recovery() == (False, None)
    assert tracker.triggered is False


def test_doom_loop_tracker_resets_on_different_args() -> None:
    tracker = _DoomLoopTracker(threshold=3)
    args = {"command": "echo hi"}
    tracker.record("run_command", args)
    tracker.record("run_command", args)
    tracker.record("run_command", {"command": "other"})
    assert tracker.streak_count == 1
    assert tracker.take_error() is None
    tracker.record("run_command", {"command": "other"})
    tracker.record("run_command", {"command": "other"})
    assert tracker.take_error() is not None


def test_doom_loop_tracker_disabled_when_threshold_zero() -> None:
    tracker = _DoomLoopTracker(threshold=0)
    args = {"command": "x"}
    for _ in range(5):
        tracker.record("run_command", args)
    assert tracker.triggered is False
    assert tracker.take_error() is None


def test_effective_doom_loop_threshold_env(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("DOOM_LOOP_THRESHOLD", raising=False)
    assert _effective_doom_loop_threshold() == 3
    monkeypatch.setenv("DOOM_LOOP_THRESHOLD", "5")
    assert _effective_doom_loop_threshold() == 5
    monkeypatch.setenv("DOOM_LOOP_THRESHOLD", "0")
    assert _effective_doom_loop_threshold() == 0
    monkeypatch.setenv("DOOM_LOOP_THRESHOLD", "nope")
    with caplog.at_level("WARNING", logger="monkeybot.core.runtime.loop"):
        assert _effective_doom_loop_threshold() == 3
    assert "invalid DOOM_LOOP_THRESHOLD" in caplog.text


@pytest.mark.asyncio
async def test_run_doom_loop_emits_error_and_forces_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOOM_LOOP_THRESHOLD", "3")
    fail_scripts = [
        [_failing_call("c1"), Done()],
        [_failing_call("c2"), Done()],
        [_failing_call("c3"), Done()],
        [TextDelta(text="I will try a different approach."), Done()],
    ]
    prov = ToolsRecordingProvider(fail_scripts)
    events = []
    async for e in run(
        "u",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(ToolExecutionResult.err("boom")),
        max_turns=10,
    ):
        events.append(e)

    doom_errors = [
        e
        for e in events
        if isinstance(e, Error) and e.error.startswith("Doom loop detected:")
    ]
    assert len(doom_errors) == 1
    assert doom_errors[0].error == _doom_loop_texts("run_command", 3)[0]
    assert isinstance(events[-1], TurnComplete)
    assert prov.stream_calls == 4
    assert prov.stream_tools[:3] == [["run_command"]] * 3
    assert prov.stream_tools[3] == []


@pytest.mark.asyncio
async def test_run_doom_loop_on_identical_successes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOOM_LOOP_THRESHOLD", "3")
    shot = [_tool_call("c1", args={}), Done()]
    prov = ToolsRecordingProvider(
        [
            shot,
            shot,
            shot,
            [TextDelta(text="I will navigate instead."), Done()],
        ]
    )
    events = []
    async for e in run(
        "u",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(ToolExecutionResult.ok_text('{"ok": true}')),
        max_turns=10,
    ):
        events.append(e)

    doom_errors = [
        e
        for e in events
        if isinstance(e, Error) and e.error.startswith("Doom loop detected:")
    ]
    assert len(doom_errors) == 1
    assert isinstance(events[-1], TurnComplete)
    assert prov.stream_tools[3] == []


@pytest.mark.asyncio
async def test_run_doom_loop_triggers_again_after_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After recovery, a second identical streak must fire again.

    Uses a whitespace-only recovery reply so the silent-model guard continues the
    same user message (a normal text reply would end the turn after the first
    recovery).
    """
    monkeypatch.setenv("DOOM_LOOP_THRESHOLD", "2")
    fail = [_failing_call("c1"), Done()]
    scripts = [
        fail,
        fail,
        [TextDelta(text="   "), Done()],
        fail,
        fail,
        [TextDelta(text="stopping for real."), Done()],
    ]
    prov = ToolsRecordingProvider(scripts)
    events = []
    async for e in run(
        "u",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(ToolExecutionResult.err("boom")),
        max_turns=20,
    ):
        events.append(e)

    doom_errors = [
        e
        for e in events
        if isinstance(e, Error) and e.error.startswith("Doom loop detected:")
    ]
    assert len(doom_errors) == 2
    assert isinstance(events[-1], TurnComplete)
    # fail, fail, recovery(no tools), fail, fail, recovery(no tools)
    assert prov.stream_tools == [
        ["run_command"],
        ["run_command"],
        [],
        ["run_command"],
        ["run_command"],
        [],
    ]


@pytest.mark.asyncio
async def test_run_doom_loop_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOOM_LOOP_THRESHOLD", "0")
    fail = [_failing_call("c1"), Done()]
    prov = ToolsRecordingProvider([fail, fail, fail, fail])
    events = []
    async for e in run(
        "u",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(ToolExecutionResult.err("boom")),
        max_turns=3,
    ):
        events.append(e)

    assert not any(
        isinstance(e, Error) and e.error.startswith("Doom loop detected:") for e in events
    )
    assert any(isinstance(e, Error) and "Max turns exceeded" in e.error for e in events)
    assert all(tools == ["run_command"] for tools in prov.stream_tools)


@pytest.mark.asyncio
async def test_run_does_not_trigger_doom_loop_when_args_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOOM_LOOP_THRESHOLD", "3")
    scripts = [
        [_tool_call("c1", args={"command": "a"}), Done()],
        [_tool_call("c2", args={"command": "a"}), Done()],
        [_tool_call("c3", args={"command": "b"}), Done()],
        [TextDelta(text="done"), Done()],
    ]
    prov = ToolsRecordingProvider(scripts)
    events = []
    async for e in run(
        "u",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(ToolExecutionResult.ok_text("ok")),
        max_turns=10,
    ):
        events.append(e)

    assert not any(
        isinstance(e, Error) and e.error.startswith("Doom loop detected:") for e in events
    )
    assert isinstance(events[-1], TurnComplete)
    assert all(tools == ["run_command"] for tools in prov.stream_tools[:3])


@pytest.mark.asyncio
async def test_run_injects_doom_loop_system_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOOM_LOOP_THRESHOLD", "2")
    fail = [_failing_call("c1"), Done()]
    snapshots: list[str] = []

    class SnapshotProvider(ToolsRecordingProvider):
        async def stream(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDef],
            *,
            model: str,
            thinking_budget: int | None = None,
            vertex_google_search: bool = False,
        ) -> AsyncIterator[ProviderEvent]:
            system = next((m for m in messages if m.role == "system"), None)
            if system is not None:
                text = "".join(
                    getattr(b, "text", "") for b in system.content if hasattr(b, "text")
                )
                snapshots.append(text)
            async for ev in super().stream(
                messages,
                tools,
                model=model,
                thinking_budget=thinking_budget,
                vertex_google_search=vertex_google_search,
            ):
                yield ev

    prov = SnapshotProvider(
        [
            fail,
            fail,
            [TextDelta(text="stopping"), Done()],
        ]
    )
    async for _ in run(
        "u",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(ToolExecutionResult.err("boom")),
        max_turns=10,
    ):
        pass

    assert any("Doom loop detected" in s for s in snapshots)
    assert prov.stream_tools[-1] == []
