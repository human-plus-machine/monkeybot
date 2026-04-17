"""Unit tests for EventBus — isolation, kind filtering, timeouts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from src.core.harness.event_bus import EventBus
from src.core.harness.events import EventKind, HarnessEvent, Principal, VersionTriple


def _event(kind: EventKind = EventKind.AGENT_START) -> HarnessEvent:
    return HarnessEvent(
        run_id="r", session_id="s",
        principal=Principal(),
        versions=VersionTriple(harness="1", deep_agents="x", model="y"),
        ts=datetime.now(UTC), kind=kind,
    )


@pytest.mark.asyncio
async def test_raising_handler_does_not_break_publish() -> None:
    bus = EventBus(include_default_logger=False)
    received: list[str] = []

    class Boom:
        name = "boom"
        async def handle(self, event): raise RuntimeError("nope")

    class Good:
        name = "good"
        async def handle(self, event): received.append(event.kind.value)

    bus.subscribe(Boom())
    bus.subscribe(Good())
    await bus.publish(_event())
    assert received == ["agent.start"]
    assert bus.stats.handler_errors == 1
    assert bus.stats.delivered == 1


@pytest.mark.asyncio
async def test_kind_filter() -> None:
    bus = EventBus(include_default_logger=False)
    received: list[str] = []

    class H:
        name = "h"
        async def handle(self, event): received.append(event.kind.value)

    bus.subscribe(H(), kinds=[EventKind.TOOL_CALL])
    await bus.publish(_event(EventKind.AGENT_START))
    await bus.publish(_event(EventKind.TOOL_CALL))
    assert received == ["tool.call"]


@pytest.mark.asyncio
async def test_timeout_enforced() -> None:
    bus = EventBus(include_default_logger=False)

    class Slow:
        name = "slow"
        async def handle(self, event): await asyncio.sleep(1.0)

    bus.subscribe(Slow(), timeout_s=0.05)
    await bus.publish(_event())
    assert bus.stats.handler_timeouts == 1
