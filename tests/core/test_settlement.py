"""Tests for hook settlement barrier (P1.2)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import Done, TextDelta, ToolCall
from monkeybot.core.runtime.events import TurnComplete
from monkeybot.core.runtime.loop import run
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.types_tools import ToolDef

from tests.core.test_hooks import _payload
from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider, _ctx as loop_ctx


@pytest.mark.asyncio
async def test_drain_settlement_awaits_background_hooks() -> None:
    mgr = HookManager()
    finished = asyncio.Event()

    async def bg(_p: HookPayload) -> None:
        await asyncio.sleep(0.05)
        finished.set()

    mgr.register(HookEvent.POST_TURN, bg)
    await mgr.fire(_payload(event=HookEvent.POST_TURN), timeout_s=0)
    assert not finished.is_set()
    await mgr.drain_settlement(timeout_s=1.0)
    assert finished.is_set()


@pytest.mark.asyncio
async def test_drain_settlement_timeout_logs_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mgr = HookManager()

    async def slow(_p: HookPayload) -> None:
        await asyncio.sleep(10)

    mgr.register(HookEvent.POST_TOOL, slow)
    await mgr.fire(_payload(event=HookEvent.POST_TOOL), timeout_s=0)

    caplog.set_level(logging.WARNING, logger="monkeybot.core.hooks")
    await mgr.drain_settlement(timeout_s=0.05)
    assert any("settlement timed out" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_turn_complete_waits_for_post_tool_settlement() -> None:
    """POST_TOOL side effects finish before TurnComplete is yielded."""
    order: list[str] = []
    mgr = HookManager()

    async def post_tool(_p: HookPayload) -> None:
        await asyncio.sleep(0.05)
        order.append("post_tool_done")

    mgr.register(HookEvent.POST_TOOL, post_tool)

    class MarkingExecutor:
        async def execute(self, *, call: ToolCall, ctx) -> ToolExecutionResult:  # type: ignore[no-untyped-def]
            del call, ctx
            return ToolExecutionResult.ok_text("ok")

    prov = FakeProvider(
        [
            [ToolCall(call_id="c1", name="read_file", args={"path": "x"}), Done()],
            [TextDelta(text="done"), Done()],
        ]
    )
    hist = FakeHistory()
    ctx = loop_ctx()
    from monkeybot.core.context import TurnContext

    ctx = TurnContext(
        **{
            **ctx.__dict__,
            "tools": [ToolDef("read_file", "r", {"type": "object"}, parallel_safe=True)],
        }
    )
    events: list[object] = []
    async for e in run(
        "u",
        ctx,
        provider=prov,
        history=hist,
        inspectors=[AllowInspector()],
        tool_executor=MarkingExecutor(),
        hook_manager=mgr,
        max_turns=4,
    ):
        if isinstance(e, TurnComplete):
            order.append("turn_complete")
        events.append(e)

    assert "post_tool_done" in order
    assert "turn_complete" in order
    assert order.index("post_tool_done") < order.index("turn_complete")
