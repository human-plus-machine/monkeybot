"""Doom-loop detection: consecutive identical tool calls (ok or error)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from monkeybot.core.llm.provider import Done, Message, ProviderEvent, TextDelta, ToolCall
from monkeybot.core.runtime.doom_loop import (
    _doom_loop_texts,
    _DoomLoopTracker,
    _effective_doom_loop_threshold,
    _tool_call_fingerprint,
)
from monkeybot.core.runtime.events import Error, HarnessIntervention, TurnComplete
from monkeybot.core.runtime.loop import run
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.types_tools import ToolDef
from testkit.context import _ctx
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory
from testkit.inspectors import AllowInspector
from testkit.provider import ToolsRecordingProvider


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


def test_doom_loop_tracker_exempts_polling_tools() -> None:
    """``doom_loop_exempt`` tools (e.g. loop_status) never trip the guard."""
    tracker = _DoomLoopTracker(
        threshold=3,
        exempt_names=frozenset({"loop_status"}),
    )
    args: dict[str, object] = {}
    for _ in range(10):
        tracker.record("loop_status", args)
    assert tracker.triggered is False
    assert tracker.take_error() is None
    assert tracker.streak_count == 0

    # Non-exempt tools still trip.
    tracker.record("run_command", {"command": "x"})
    tracker.record("run_command", {"command": "x"})
    tracker.record("run_command", {"command": "x"})
    assert tracker.take_error() is not None


def test_await_command_polling_is_doom_loop_exempt() -> None:
    """Repeated await_command on a running job must not trip the guard."""
    tracker = _DoomLoopTracker(
        threshold=3,
        exempt_names=frozenset({"await_command"}),
    )
    args = {"job_id": "job1", "wait_seconds": 300, "cursor": 0}
    for _ in range(6):
        tracker.record("await_command", args)
    assert tracker.triggered is False


def test_doom_loop_exempt_names_from_tool_defs() -> None:
    from monkeybot.core.runtime.doom_loop import _doom_loop_exempt_names

    tools = [
        ToolDef("read_file", "r", {"type": "object"}, parallel_safe=True),
        ToolDef(
            "loop_status",
            "s",
            {"type": "object"},
            parallel_safe=True,
            doom_loop_exempt=True,
        ),
    ]
    assert _doom_loop_exempt_names(tools) == frozenset({"loop_status"})


@pytest.mark.asyncio
async def test_run_does_not_doom_loop_on_loop_status_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documented start_loop / loop_status polling must not force recovery."""
    monkeypatch.setenv("DOOM_LOOP_THRESHOLD", "3")

    def _status_call(call_id: str) -> ToolCall:
        return ToolCall(call_id=call_id, name="loop_status", args={})

    scripts = [
        [_status_call("c1"), Done()],
        [_status_call("c2"), Done()],
        [_status_call("c3"), Done()],
        [_status_call("c4"), Done()],
        [TextDelta(text="still waiting, will check again later."), Done()],
    ]
    prov = ToolsRecordingProvider(scripts)
    ctx = _ctx()
    from monkeybot.core.context import TurnContext

    ctx = TurnContext(
        **{
            **ctx.__dict__,
            "tools": [
                ToolDef(
                    "loop_status",
                    "status",
                    {"type": "object"},
                    parallel_safe=True,
                    doom_loop_exempt=True,
                )
            ],
        }
    )
    events = []
    async for e in run(
        "u",
        ctx,
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(ToolExecutionResult.ok_text('{"status":"running"}')),
        max_turns=10,
    ):
        events.append(e)

    assert not any(
        isinstance(e, Error) and e.error.startswith("Doom loop detected:") for e in events
    )
    assert isinstance(events[-1], TurnComplete)
    # Four status polls + final text reply — tools available on every status turn.
    assert prov.stream_tools[:4] == [["loop_status"]] * 4
    assert prov.stream_tools[4] == ["loop_status"]


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
    with caplog.at_level("WARNING", logger="monkeybot.core.runtime.loop.doom_loop"):
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
        e for e in events if isinstance(e, Error) and e.error.startswith("Doom loop detected:")
    ]
    assert len(doom_errors) == 1
    assert doom_errors[0].error == _doom_loop_texts("run_command", 3)[0]
    interventions = [
        e for e in events if isinstance(e, HarnessIntervention) and e.intervention == "doom_loop"
    ]
    assert len(interventions) == 1
    assert interventions[0].detail == doom_errors[0].error
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
        e for e in events if isinstance(e, Error) and e.error.startswith("Doom loop detected:")
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
        e for e in events if isinstance(e, Error) and e.error.startswith("Doom loop detected:")
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
                text = "".join(getattr(b, "text", "") for b in system.content if hasattr(b, "text"))
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


# --- same tool + same path failing, other args differing ---


def _edit_args(old: str) -> dict[str, object]:
    return {"path": "calc/ops.py", "old_string": old, "new_string": "x"}


def test_path_failure_streak_triggers_even_when_other_args_differ() -> None:
    tracker = _DoomLoopTracker(threshold=3)
    for i in range(2):
        tracker.record("replace_in_file", _edit_args(f"guess {i}"), failed=True)
        assert not tracker.triggered
    tracker.record("replace_in_file", _edit_args("guess 3"), failed=True)
    assert tracker.triggered and tracker.force_no_tools
    assert tracker.triggered_tool == "replace_in_file"
    error = tracker.take_error()
    assert error is not None and "failed 3 times in a row on 'calc/ops.py'" in error
    assert tracker.recovery_note is not None
    assert "Re-read the file" in tracker.recovery_note
    # Same recovery path as the identical-call detector: one tool-less turn, then re-armed.
    force, note = tracker.consume_recovery()
    assert force and note and not tracker.triggered
    assert tracker.path_streak_count == 0


def test_path_failure_streak_is_broken_by_success_other_path_or_other_call() -> None:
    def tracker_after(*steps: tuple[str, dict[str, object], bool]) -> _DoomLoopTracker:
        t = _DoomLoopTracker(threshold=3)
        for name, args, failed in steps:
            t.record(name, args, failed=failed)
        return t

    fail_a = ("replace_in_file", _edit_args("a"), True)
    fail_b = ("replace_in_file", _edit_args("b"), True)
    ok = ("replace_in_file", _edit_args("c"), False)
    other_path = ("replace_in_file", {"path": "calc/other.py", "old_string": "q"}, True)
    other_tool = ("read_file", {"path": "calc/ops.py"}, False)
    other_tool_failing = ("write_file", {"path": "calc/ops.py", "content": "z"}, True)

    assert not tracker_after(fail_a, fail_b, ok, fail_a).triggered
    assert not tracker_after(fail_a, fail_b, other_path, fail_a).triggered
    assert not tracker_after(fail_a, fail_b, other_tool, fail_a).triggered
    assert not tracker_after(fail_a, fail_b, other_tool_failing, fail_a).triggered
    assert tracker_after(fail_a, fail_b, fail_a).triggered


def test_path_streak_ignores_calls_without_a_path_or_that_succeed() -> None:
    tracker = _DoomLoopTracker(threshold=2)
    tracker.record("run_command", {"command": "a"}, failed=True)
    tracker.record("run_command", {"command": "b"}, failed=True)
    assert not tracker.triggered  # no path argument: only the identical-call detector applies
    tracker.record("replace_in_file", _edit_args("a"), failed=False)
    tracker.record("replace_in_file", _edit_args("b"), failed=False)
    assert not tracker.triggered


def test_path_streak_respects_disabled_threshold_and_exempt_tools() -> None:
    off = _DoomLoopTracker(threshold=0)
    for i in range(5):
        off.record("replace_in_file", _edit_args(str(i)), failed=True)
    assert not off.triggered
    exempt = _DoomLoopTracker(threshold=2, exempt_names=frozenset({"replace_in_file"}))
    for i in range(5):
        exempt.record("replace_in_file", _edit_args(str(i)), failed=True)
    assert not exempt.triggered


def test_identical_calls_still_use_the_original_message() -> None:
    tracker = _DoomLoopTracker(threshold=3)
    for _ in range(3):
        tracker.record("replace_in_file", _edit_args("same"), failed=True)
    assert tracker.take_error() == _doom_loop_texts("replace_in_file", 3)[0]


@pytest.mark.asyncio
async def test_run_stops_a_retry_loop_of_slightly_different_failing_edits() -> None:
    script = [
        [ToolCall(call_id=f"c{i}", name="replace_in_file", args=_edit_args(f"guess {i}")), Done()]
        for i in range(3)
    ] + [[TextDelta(text="I'm stuck on calc/ops.py: re-read it and found the real text."), Done()]]
    prov = ToolsRecordingProvider(script)
    events = [
        e
        async for e in run(
            "fix it",
            _ctx(),
            provider=prov,
            history=FakeHistory(),
            inspectors=[AllowInspector()],
            tool_executor=RecordingExecutor((None, '{"ok": false, "error_kind": "validation"}')),
            max_turns=8,
        )
    ]
    gates = [
        e for e in events if isinstance(e, HarnessIntervention) and e.intervention == "doom_loop"
    ]
    assert len(gates) == 1 and "failed 3 times in a row" in gates[0].detail
    assert any(isinstance(e, Error) and "failed 3 times" in e.error for e in events)
    assert prov.stream_tools[-1] == []  # the recovery turn has no tools
    assert isinstance(events[-1], TurnComplete)


def test_path_streak_treats_workspace_spellings_as_the_same_file() -> None:
    tracker = _DoomLoopTracker(threshold=3)
    variants = (
        {"path": "./calc/ops.py", "old_string": "a"},
        {"file_path": "calc/./ops.py", "old_string": "b"},
        {"file": "calc\\ops.py", "old_string": "c"},
        {"path": "calc/../calc/ops.py", "old_string": "d"},
    )
    for args in variants[:2]:
        tracker.record("replace_in_file", args, failed=True)
        assert not tracker.triggered
    tracker.record("replace_in_file", variants[2], failed=True)
    assert tracker.triggered
    error = tracker.take_error()
    assert error is not None and "failed 3 times in a row on 'calc/ops.py'" in error
    # Re-armed, a parent-relative spelling is still that file.
    tracker.consume_recovery()
    for args in variants[1:]:
        tracker.record("replace_in_file", args, failed=True)
    assert tracker.triggered
    assert "calc/ops.py" in (tracker.take_error() or "")


def test_absolute_path_does_not_share_a_streak_with_the_relative_spelling() -> None:
    tracker = _DoomLoopTracker(threshold=2)
    tracker.record(
        "replace_in_file",
        {"path": "/calc/ops.py", "old_string": "a"},
        failed=True,
    )
    tracker.record(
        "replace_in_file",
        {"path": "calc/ops.py", "old_string": "b"},
        failed=True,
    )
    assert not tracker.triggered


def test_exempt_polling_does_not_hide_or_trip_a_path_streak() -> None:
    tracker = _DoomLoopTracker(threshold=3, exempt_names=frozenset({"loop_status"}))
    tracker.record("replace_in_file", _edit_args("a"), failed=True)
    tracker.record("loop_status", {}, failed=False)
    tracker.record("replace_in_file", _edit_args("b"), failed=True)
    tracker.record("loop_status", {"loop_id": "x"}, failed=True)
    assert not tracker.triggered
    tracker.record("replace_in_file", _edit_args("c"), failed=True)
    assert tracker.triggered


def _patch(path: str, old: str) -> dict[str, object]:
    return {
        "patch_text": (f"*** Begin Patch\n*** Update File: {path}\n@@\n-{old}\n+y\n*** End Patch\n")
    }


def test_apply_patch_retries_on_one_file_trip_the_path_streak() -> None:
    tracker = _DoomLoopTracker(threshold=3)
    for spelling, old in (
        ("./calc/ops.py", "a"),
        ("calc/./ops.py", "b"),
        ("calc/../calc/ops.py", "c"),
    ):
        tracker.record("apply_patch", _patch(spelling, old), failed=True)
    assert tracker.triggered
    assert "calc/ops.py" in (tracker.take_error() or "")


def test_multi_file_patch_breaks_a_single_file_streak() -> None:
    tracker = _DoomLoopTracker(threshold=2)
    tracker.record("apply_patch", _patch("calc/ops.py", "a"), failed=True)
    tracker.record(
        "apply_patch",
        {
            "patch_text": (
                "*** Begin Patch\n"
                "*** Update File: calc/ops.py\n"
                "@@\n-a\n+b\n"
                "*** Update File: calc/other.py\n"
                "@@\n-c\n+d\n"
                "*** End Patch\n"
            )
        },
        failed=True,
    )
    tracker.record("apply_patch", _patch("calc/ops.py", "e"), failed=True)
    assert not tracker.triggered
