"""Tests for :mod:`monkeybot.core.hooks`."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload


def _ctx() -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="agent",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
    )


def _payload(event: HookEvent = HookEvent.PRE_TURN, **kw: Any) -> HookPayload:
    return HookPayload(event=event, thread_id="t1", request_id="r1", ctx=_ctx(), **kw)


@pytest.mark.asyncio
async def test_fire_with_no_handlers_returns_payload_unchanged() -> None:
    mgr = HookManager()
    p = _payload()

    out = await mgr.fire(p)

    assert out is p
    assert out.inject_text is None
    assert out.inject_memory_lines == []


@pytest.mark.asyncio
async def test_handlers_run_in_registration_order_and_see_shared_payload() -> None:
    mgr = HookManager()
    order: list[str] = []

    async def first(p: HookPayload) -> None:
        order.append("first")
        p.inject_text = "A"

    async def second(p: HookPayload) -> None:
        order.append("second")
        assert p.inject_text == "A"
        p.inject_text = (p.inject_text or "") + "B"

    mgr.register(HookEvent.PRE_TURN, first)
    mgr.register(HookEvent.PRE_TURN, second)

    out = await mgr.fire(_payload())

    assert order == ["first", "second"]
    assert out.inject_text == "AB"


@pytest.mark.asyncio
async def test_handler_for_other_event_does_not_fire() -> None:
    mgr = HookManager()
    calls: list[HookEvent] = []

    async def h(p: HookPayload) -> None:
        calls.append(p.event)

    mgr.register(HookEvent.PRE_TURN, h)

    await mgr.fire(_payload(event=HookEvent.POST_TURN))

    assert calls == []


@pytest.mark.asyncio
async def test_timeout_is_swallowed_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    mgr = HookManager()

    async def slow(_p: HookPayload) -> None:
        await asyncio.sleep(10)

    mgr.register(HookEvent.PRE_TURN, slow)

    caplog.set_level(logging.WARNING, logger="monkeybot.core.hooks")
    out = await mgr.fire(_payload(), timeout_s=0.05)

    assert out.inject_text is None
    assert any("timed out" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_handler_exception_is_swallowed_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mgr = HookManager()

    async def boom(_p: HookPayload) -> None:
        raise RuntimeError("nope")

    async def after(p: HookPayload) -> None:
        p.inject_text = "ran"

    mgr.register(HookEvent.PRE_TURN, boom)
    mgr.register(HookEvent.PRE_TURN, after)

    caplog.set_level(logging.WARNING, logger="monkeybot.core.hooks")
    out = await mgr.fire(_payload())

    assert out.inject_text == "ran"
    assert any("hook error" in r.message and "nope" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_recursion_is_blocked_inside_hook() -> None:
    mgr = HookManager()
    inner_calls: list[HookEvent] = []

    async def inner(p: HookPayload) -> None:
        inner_calls.append(p.event)

    async def outer(_p: HookPayload) -> None:
        await mgr.fire(_payload(event=HookEvent.POST_TOOL))

    mgr.register(HookEvent.PRE_TURN, outer)
    mgr.register(HookEvent.POST_TOOL, inner)

    await mgr.fire(_payload(event=HookEvent.PRE_TURN))

    assert inner_calls == [], "nested fire() must skip handlers"


@pytest.mark.asyncio
async def test_timeout_zero_returns_immediately_and_runs_in_background() -> None:
    mgr = HookManager()
    started = asyncio.Event()
    finished = asyncio.Event()

    async def bg(_p: HookPayload) -> None:
        started.set()
        await asyncio.sleep(0.02)
        finished.set()

    mgr.register(HookEvent.POST_TURN, bg)

    await mgr.fire(_payload(event=HookEvent.POST_TURN), timeout_s=0)

    assert not finished.is_set(), "background hook must not be awaited"
    await asyncio.wait_for(finished.wait(), timeout=1.0)
    assert started.is_set()


@pytest.mark.asyncio
async def test_background_exception_is_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mgr = HookManager()

    async def boom(_p: HookPayload) -> None:
        raise RuntimeError("bg-fail")

    mgr.register(HookEvent.POST_TURN, boom)

    caplog.set_level(logging.WARNING, logger="monkeybot.core.hooks")
    await mgr.fire(_payload(event=HookEvent.POST_TURN), timeout_s=0)

    await asyncio.sleep(0.05)
    assert any(
        "background hook error" in r.message and "bg-fail" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_clear_removes_handlers() -> None:
    mgr = HookManager()
    calls: list[str] = []

    async def h(_p: HookPayload) -> None:
        calls.append("x")

    mgr.register(HookEvent.PRE_TURN, h)
    mgr.register(HookEvent.POST_TURN, h)

    mgr.clear(HookEvent.PRE_TURN)
    await mgr.fire(_payload(event=HookEvent.PRE_TURN))
    assert calls == []

    await mgr.fire(_payload(event=HookEvent.POST_TURN))
    assert calls == ["x"]

    mgr.clear()
    calls.clear()
    await mgr.fire(_payload(event=HookEvent.POST_TURN))
    assert calls == []


@pytest.mark.asyncio
async def test_inject_memory_lines_accumulate_across_handlers() -> None:
    mgr = HookManager()

    async def first(p: HookPayload) -> None:
        p.inject_memory_lines.append("- one")

    async def second(p: HookPayload) -> None:
        p.inject_memory_lines.append("- two")

    mgr.register(HookEvent.PRE_TURN, first)
    mgr.register(HookEvent.PRE_TURN, second)

    out = await mgr.fire(_payload())

    assert out.inject_memory_lines == ["- one", "- two"]
