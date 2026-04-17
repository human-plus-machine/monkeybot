"""Unit tests for ContextPolicyMW."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.core.harness.event_bus import EventBus
from src.core.harness.events import EventKind, Principal, VersionTriple
from src.core.harness.middleware.context_policy import ContextPolicyMW
from src.core.harness.specs import ContextPolicySpec


class _Recorder:
    name = "rec"

    def __init__(self) -> None:
        self.events: list[str] = []

    async def handle(self, event):
        self.events.append(event.kind.value)


def _mkargs(bus: EventBus) -> dict:
    return dict(
        run_id="r",
        session_id="s",
        principal=Principal(),
        versions=VersionTriple(harness="1", deep_agents="x", model="gemini-2.5-flash"),
    )


@pytest.mark.asyncio
async def test_utilization_event_fires() -> None:
    bus = EventBus(include_default_logger=False)
    rec = _Recorder()
    bus.subscribe(rec)
    mw = ContextPolicyMW(ContextPolicySpec(token_budget=100), bus)
    await mw.apply(["hello world"], **_mkargs(bus))
    assert "budget.utilization" in rec.events


@pytest.mark.asyncio
async def test_hard_reset_fires() -> None:
    bus = EventBus(include_default_logger=False)
    rec = _Recorder()
    bus.subscribe(rec)
    mw = ContextPolicyMW(ContextPolicySpec(token_budget=10, summarize_at=0.5, hard_reset_at=0.7), bus)
    out = await mw.apply(["a" * 1000], **_mkargs(bus))
    assert EventKind.CONTEXT_RESET.value in rec.events
    assert len(out) == 1
    assert out[0]["role"] == "system"


@pytest.mark.asyncio
async def test_summarize_fires_when_summarizer_provided() -> None:
    bus = EventBus(include_default_logger=False)
    rec = _Recorder()
    bus.subscribe(rec)

    async def summarizer(msgs):
        return [{"role": "system", "content": "summary"}]

    mw = ContextPolicyMW(
        ContextPolicySpec(token_budget=30, summarize_at=0.5, hard_reset_at=0.95),
        bus,
        summarizer=summarizer,
    )
    out = await mw.apply(["a" * 100], **_mkargs(bus))
    assert EventKind.CONTEXT_SUMMARIZE.value in rec.events
    assert out == [{"role": "system", "content": "summary"}]
