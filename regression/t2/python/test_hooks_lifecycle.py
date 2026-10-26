"""T2-HOOK-01..07: hook order, timeouts, and compaction settlement.

Settlement is ``HookManager.drain_settlement``. These tests do not sleep.
T2-HOOK-04 waits on the product hook timeout (2s), which abandons the hook.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import Done, Message, TextDelta, ToolCall
from monkeybot.core.runtime.loop import run
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.types.types_tools import ToolDef
from testkit.context import _ctx
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory
from testkit.hooks import RecordingHook
from testkit.provider import FakeProvider, ToolsRecordingProvider

_ORDER = [
    HookEvent.USER_MESSAGE,
    HookEvent.PRE_TURN,
    HookEvent.TOOL_DEFINITION,
    HookEvent.BEFORE_PROVIDER_REQUEST,
    HookEvent.PRE_TOOL,
    HookEvent.AFTER_PROVIDER_RESPONSE,
    HookEvent.POST_TOOL,
    HookEvent.PRE_FINAL_ANSWER,
    HookEvent.POST_TURN,
]


def _tool_then_text() -> list[list[object]]:
    return [
        [ToolCall(call_id="c1", name="read_file", args={"path": "DONE.txt"}), Done()],
        [TextDelta(text="finished"), Done()],
    ]


def _padded(count: int) -> list[Message]:
    return [
        Message(
            role="user" if index % 2 == 0 else "assistant",
            content=[Text(text="z" * 600)],
        )
        for index in range(count)
    ]


def _blob(messages: list[Message]) -> str:
    parts: list[str] = []
    for message in messages:
        for block in message.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _assert_subsequence(actual: list[HookEvent], expected: list[HookEvent]) -> None:
    cursor = 0
    for event in actual:
        if cursor < len(expected) and event == expected[cursor]:
            cursor += 1
    missing = expected[cursor:]
    assert not missing, f"hook order {actual} missing {missing}"


async def _run(
    script: list[list[object]],
    manager: HookManager,
    *,
    ctx: TurnContext | None = None,
    history: FakeHistory | None = None,
    provider: FakeProvider | None = None,
) -> tuple[list[object], FakeProvider, FakeHistory]:
    active = _ctx() if ctx is None else ctx
    hist = FakeHistory() if history is None else history
    prov = FakeProvider(script) if provider is None else provider
    events: list[object] = []
    async for event in run(
        "hello",
        active,
        provider=prov,
        history=hist,
        inspectors=[],
        tool_executor=RecordingExecutor(),
        hook_manager=manager,
    ):
        events.append(event)
    return events, prov, hist


def _completed(events: list[object]) -> bool:
    return any(getattr(event, "kind", None) == "TurnComplete" for event in events)


def _assistant(events: list[object]) -> str:
    return "".join(
        getattr(event, "delta", "")
        for event in events
        if getattr(event, "kind", None) == "AssistantDelta"
    )


@pytest.mark.case("T2-HOOK-01")
async def test_hook_lifecycle_order() -> None:
    manager = HookManager()
    recorder = RecordingHook()
    recorder.register(manager)
    events, _, _ = await _run(_tool_then_text(), manager)
    await manager.drain_settlement(timeout_s=2.0)
    _assert_subsequence(recorder.events, _ORDER)
    assert _completed(events)


@pytest.mark.case("T2-HOOK-02")
async def test_pre_turn_once() -> None:
    manager = HookManager()
    recorder = RecordingHook()
    recorder.register(manager, [HookEvent.PRE_TURN])
    events, _, _ = await _run(_tool_then_text(), manager)
    assert recorder.events == [HookEvent.PRE_TURN]
    assert _completed(events)


@pytest.mark.case("T2-HOOK-03")
async def test_hook_exception_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def boom(hook_payload: HookPayload) -> None:
        del hook_payload
        raise RuntimeError("hook-boom")

    manager = HookManager()
    manager.register(HookEvent.PRE_TURN, boom)
    with caplog.at_level(logging.WARNING, logger="monkeybot.core.hooks"):
        events, _, _ = await _run([[TextDelta(text="still here"), Done()]], manager)
    assert _completed(events)
    assert "still here" in _assistant(events)
    assert "hook error" in caplog.text
    assert "hook-boom" in caplog.text


@pytest.mark.case("T2-HOOK-04")
async def test_hook_timeout_continues(caplog: pytest.LogCaptureFixture) -> None:
    finished = False
    gate = asyncio.Event()

    async def hang(hook_payload: HookPayload) -> None:
        nonlocal finished
        del hook_payload
        await gate.wait()
        finished = True

    manager = HookManager()
    manager.register(HookEvent.PRE_TURN, hang)
    with caplog.at_level(logging.WARNING, logger="monkeybot.core.hooks"):
        events, _, _ = await _run([[TextDelta(text="continued"), Done()]], manager)
    assert _completed(events)
    assert "continued" in _assistant(events)
    assert finished is False
    assert "timed out" in caplog.text


@pytest.mark.case("T2-HOOK-05")
async def test_settlement_before_turn_complete() -> None:
    order: list[str] = []

    async def post_tool(hook_payload: HookPayload) -> None:
        del hook_payload
        order.append("post_tool_started")
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        loop.call_soon(waiter.set_result, None)
        await waiter
        order.append("post_tool_done")

    manager = HookManager()
    manager.register(HookEvent.POST_TOOL, post_tool)
    events: list[object] = []
    async for event in run(
        "hello",
        _ctx(),
        provider=FakeProvider(_tool_then_text()),
        history=FakeHistory(),
        inspectors=[],
        tool_executor=RecordingExecutor(),
        hook_manager=manager,
    ):
        if getattr(event, "kind", None) == "TurnComplete":
            order.append("turn_complete")
        events.append(event)
    assert order.index("post_tool_done") < order.index("turn_complete")
    assert _completed(events)


@pytest.mark.case("T2-HOOK-06")
async def test_tool_definition_replacement() -> None:
    async def drop_shell(hook_payload: HookPayload) -> None:
        if hook_payload.tools is None:
            return
        hook_payload.tools = [tool for tool in hook_payload.tools if tool.name != "run_command"]

    manager = HookManager()
    manager.register(HookEvent.TOOL_DEFINITION, drop_shell)
    ctx = replace(
        _ctx(),
        tools=[
            ToolDef("run_command", "Run shell", {}),
            ToolDef("read_file", "Read a file", {}),
        ],
    )
    provider = ToolsRecordingProvider([[TextDelta(text="tools replaced"), Done()]])
    events, _, _ = await _run([], manager, ctx=ctx, provider=provider)
    assert provider.stream_tools == [["read_file"]]
    assert _completed(events)


@pytest.mark.case("T2-HOOK-07")
async def test_history_compacted_awaited() -> None:
    provider = FakeProvider(
        [[TextDelta(text=" compressed summary "), Done()], [TextDelta(text="final"), Done()]]
    )
    seen: list[tuple[int, int | None]] = []

    async def on_compacted(hook_payload: HookPayload) -> None:
        seen.append((provider.stream_calls, hook_payload.turns_summarized))

    manager = HookManager()
    manager.register(HookEvent.HISTORY_COMPACTED, on_compacted)
    events, _, _ = await _run(
        [],
        manager,
        ctx=_ctx(context_window_tokens=800),
        history=FakeHistory(_padded(8)),
        provider=provider,
    )
    assert seen, "HISTORY_COMPACTED did not fire"
    calls_at_hook, summarized = seen[0]
    assert summarized is not None and summarized > 0
    assert provider.stream_calls > calls_at_hook
    assert calls_at_hook < len(provider.stream_messages)
    assert "Create a structured summary" not in _blob(provider.stream_messages[calls_at_hook])
    assert _completed(events)
