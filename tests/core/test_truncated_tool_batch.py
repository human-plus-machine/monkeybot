"""Reject truncated / incomplete tool-call batches."""

from __future__ import annotations

import pytest

from monkeybot.core.llm.provider import Done, TextDelta, ToolCall
from monkeybot.core.runtime.events import AssistantDelta, Error, ToolCallResult, TurnComplete
from monkeybot.core.runtime.loop import run
from monkeybot.core.runtime.tool_batch import (
    _rejected_tool_batch_error,
    _should_reject_tool_batch,
)
from monkeybot.core.tools.types import ToolExecutionResult
from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider, RecordingExecutor, _ctx


def test_should_reject_when_stream_truncated() -> None:
    calls = [ToolCall(call_id="c1", name="run_command", args={"command": "echo hi"})]
    assert _should_reject_tool_batch(calls, truncated=True) is True


def test_should_reject_when_all_parse_errors() -> None:
    calls = [
        ToolCall(call_id="a", name="run_command", args={}, parse_error="bad a"),
        ToolCall(call_id="b", name="run_command", args={}, parse_error="bad b"),
    ]
    assert _should_reject_tool_batch(calls, truncated=False) is True


def test_should_not_reject_mixed_batch() -> None:
    calls = [
        ToolCall(call_id="a", name="run_command", args={}, parse_error="bad"),
        ToolCall(call_id="b", name="run_command", args={"command": "ok"}),
    ]
    assert _should_reject_tool_batch(calls, truncated=False) is False


def test_should_not_reject_empty() -> None:
    assert _should_reject_tool_batch([], truncated=True) is False


def test_rejected_tool_batch_error_truncated_message() -> None:
    call = ToolCall(call_id="c1", name="run_command", args={"command": "x"})
    msg = _rejected_tool_batch_error(call, truncated=True)
    assert "output token limit" in msg
    assert "run_command" in msg
    assert "truncated" in msg


@pytest.mark.asyncio
async def test_run_rejects_truncated_batch_without_executing() -> None:
    """Done(truncated=True) fails every tool even when args look valid (Pi)."""
    prov = FakeProvider(
        [
            [
                ToolCall(
                    call_id="c1",
                    name="run_command",
                    args={"command": "rm -rf /"},  # would be dangerous if executed
                ),
                Done(truncated=True),
            ],
            [TextDelta(text="I will retry with shorter args."), Done()],
        ]
    )
    exe = RecordingExecutor(ToolExecutionResult.ok_text("should not run"))
    events = []
    async for e in run(
        "u",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=5,
    ):
        events.append(e)

    assert exe.calls == []
    results = [e for e in events if isinstance(e, ToolCallResult)]
    assert len(results) == 1
    assert results[0].error is not None
    assert "output token limit" in results[0].error
    assert any(isinstance(e, AssistantDelta) and "retry" in e.delta for e in events)
    assert isinstance(events[-1], TurnComplete)


@pytest.mark.asyncio
async def test_run_rejects_all_parse_error_batch_without_executing() -> None:
    prov = FakeProvider(
        [
            [
                ToolCall(
                    call_id="a",
                    name="run_command",
                    args={},
                    parse_error="malformed tool args JSON: a",
                ),
                ToolCall(
                    call_id="b",
                    name="run_command",
                    args={},
                    parse_error="malformed tool args JSON: b",
                ),
                Done(),
            ],
            [TextDelta(text="recovered"), Done()],
        ]
    )
    exe = RecordingExecutor()
    events = []
    async for e in run(
        "u",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=5,
    ):
        events.append(e)

    assert exe.calls == []
    errors = [
        e.error
        for e in events
        if isinstance(e, ToolCallResult) and isinstance(e.error, str)
    ]
    assert errors == [
        "malformed tool args JSON: a",
        "malformed tool args JSON: b",
    ]
    assert any(isinstance(e, AssistantDelta) and e.delta == "recovered" for e in events)
    assert not any(isinstance(e, Error) and "Max turns" in e.error for e in events)


@pytest.mark.asyncio
async def test_run_executes_valid_call_when_sibling_has_parse_error() -> None:
    """Partial parse_error in a batch must not block the valid sibling call."""
    prov = FakeProvider(
        [
            [
                ToolCall(
                    call_id="a",
                    name="run_command",
                    args={},
                    parse_error="malformed tool args JSON: a",
                ),
                ToolCall(
                    call_id="b",
                    name="run_command",
                    args={"command": "echo ok"},
                ),
                Done(),
            ],
            [TextDelta(text="done"), Done()],
        ]
    )
    exe = RecordingExecutor(ToolExecutionResult.ok_text("ok"))
    events = []
    async for e in run(
        "u",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=exe,
        max_turns=5,
    ):
        events.append(e)

    assert len(exe.calls) == 1
    assert exe.calls[0].call_id == "b"
    assert isinstance(events[-1], TurnComplete)


@pytest.mark.asyncio
async def test_await_user_response_uses_pinned_config_timeout() -> None:
    import asyncio
    from types import SimpleNamespace

    from monkeybot.core.runtime.tool_batch import _await_user_response_any

    class Bus:
        def abandon_pending_timeout(self, key: str) -> None:
            del key

    pinned = SimpleNamespace(env_values={"PENDING_RESPONSE_TIMEOUT_SEC": "0.05"})
    fut: asyncio.Future[object] = asyncio.get_running_loop().create_future()
    result = await _await_user_response_any(Bus(), fut, "k", config=pinned)  # type: ignore[arg-type]
    assert result == {"_timeout": True}
