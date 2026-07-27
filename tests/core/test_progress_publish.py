"""Unit tests for nested subagent progress publish helpers (Story 2)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from monkeybot.core.runtime.events import (
    AgentEvent,
    AssistantDelta,
    SubagentEvent,
    SystemPromptSnapshot,
    ToolCallResult,
    ToolCallStarted,
)
from monkeybot.core.subagents.progress_publish import (
    AssistantDeltaCoalescer,
    safe_publish,
)

_CORRELATION: dict[str, str | None] = {
    "request_id": "parent-req",
    "parent_call_id": "task-call-1",
    "run_id": "run-uuid",
    "child_thread_id": "subagent:t:abc1234567",
    "subagent_type": "researcher",
}


class FakePublisher:
    """Test double for EventPublisherPort."""

    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[AgentEvent] = []
        self.fail = fail
        self.call_count = 0

    async def publish_event(self, event: AgentEvent) -> None:
        self.call_count += 1
        if self.fail:
            raise RuntimeError("publish boom")
        self.events.append(event)


@pytest.mark.asyncio
async def test_safe_publish_noop_when_publisher_none() -> None:
    flag = [0]
    await safe_publish(
        None,
        AssistantDelta(request_id="r", delta="hi"),
        fail_count=flag,
        run_id="run-1",
    )
    assert flag == [0]


@pytest.mark.asyncio
async def test_safe_publish_swallows_publisher_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pub = FakePublisher(fail=True)
    flag = [0]
    evt = AssistantDelta(request_id="r", delta="x")
    with caplog.at_level(logging.WARNING):
        await safe_publish(pub, evt, fail_count=flag, run_id="run-1")
        await safe_publish(pub, evt, fail_count=flag, run_id="run-1")
    assert flag == [2]
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_safe_publish_warns_every_nth_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from monkeybot.core.subagents.progress_publish import PUBLISH_WARN_EVERY

    pub = FakePublisher(fail=True)
    flag = [0]
    evt = AssistantDelta(request_id="r", delta="x")
    with caplog.at_level(logging.WARNING):
        for _ in range(PUBLISH_WARN_EVERY):
            await safe_publish(pub, evt, fail_count=flag, run_id="run-1")
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    # 1st + Nth (e.g. 25) → two warnings
    assert flag == [PUBLISH_WARN_EVERY]
    assert len(warnings) == 2


@pytest.mark.asyncio
async def test_coalesce_flushes_at_512_chars() -> None:
    pub = FakePublisher()
    flag = [0]
    coalescer = AssistantDeltaCoalescer(
        publisher=pub,
        correlation=_CORRELATION,
        fail_count=flag,
        coalesce_ms=60_000,
        coalesce_chars=512,
    )
    chunk = "a" * 64
    for _ in range(8):
        await coalescer.handle(AssistantDelta(request_id="r", delta=chunk))
    assert len(pub.events) == 1
    wrapped = pub.events[0]
    assert isinstance(wrapped, SubagentEvent)
    assert isinstance(wrapped.inner, AssistantDelta)
    assert wrapped.inner.delta == "a" * 512


@pytest.mark.asyncio
async def test_coalesce_flushes_on_timer() -> None:
    pub = FakePublisher()
    flag = [0]
    coalescer = AssistantDeltaCoalescer(
        publisher=pub,
        correlation=_CORRELATION,
        fail_count=flag,
        coalesce_ms=20,
        coalesce_chars=512,
    )
    await coalescer.handle(AssistantDelta(request_id="r", delta="hello"))
    await coalescer.handle(AssistantDelta(request_id="r", delta=" world"))
    assert pub.events == []
    await asyncio.sleep(0.05)
    assert len(pub.events) == 1
    wrapped = pub.events[0]
    assert isinstance(wrapped, SubagentEvent)
    assert isinstance(wrapped.inner, AssistantDelta)
    assert wrapped.inner.delta == "hello world"


@pytest.mark.asyncio
async def test_coalesce_flushes_on_tool_boundary() -> None:
    pub = FakePublisher()
    flag = [0]
    coalescer = AssistantDeltaCoalescer(
        publisher=pub,
        correlation=_CORRELATION,
        fail_count=flag,
        coalesce_ms=60_000,
        coalesce_chars=512,
    )
    await coalescer.handle(AssistantDelta(request_id="r", delta="partial"))
    await coalescer.handle(
        ToolCallStarted(
            request_id="r",
            tool="search",
            label="search",
            args={},
            call_id="c1",
        )
    )
    assert len(pub.events) == 2
    assert isinstance(pub.events[0], SubagentEvent)
    assert isinstance(pub.events[0].inner, AssistantDelta)
    assert pub.events[0].inner.delta == "partial"
    assert isinstance(pub.events[1], SubagentEvent)
    assert isinstance(pub.events[1].inner, ToolCallStarted)
    assert pub.events[1].inner.tool == "search"


@pytest.mark.asyncio
async def test_coalesce_truncates_tool_result_inner() -> None:
    pub = FakePublisher()
    flag = [0]
    coalescer = AssistantDeltaCoalescer(
        publisher=pub,
        correlation=_CORRELATION,
        fail_count=flag,
        coalesce_ms=60_000,
        coalesce_chars=512,
    )
    long_result = "x" * 1000
    await coalescer.handle(
        ToolCallResult(
            request_id="r",
            tool="read_file",
            result=long_result,
            call_id="c1",
        )
    )
    assert len(pub.events) == 1
    wrapped = pub.events[0]
    assert isinstance(wrapped, SubagentEvent)
    assert isinstance(wrapped.inner, ToolCallResult)
    assert len(wrapped.inner.result) <= 601
    assert wrapped.inner.result.endswith("…")
    assert wrapped.inner.result.startswith("x" * 600)


@pytest.mark.asyncio
async def test_coalesce_drops_non_forwardable() -> None:
    pub = FakePublisher()
    flag = [0]
    coalescer = AssistantDeltaCoalescer(
        publisher=pub,
        correlation=_CORRELATION,
        fail_count=flag,
        coalesce_ms=60_000,
        coalesce_chars=512,
    )
    await coalescer.handle(
        SystemPromptSnapshot(request_id="r", inner_turn=1, text="## Agent")
    )
    assert pub.events == []


@pytest.mark.asyncio
async def test_aclose_flushes_remainder() -> None:
    pub = FakePublisher()
    flag = [0]
    coalescer = AssistantDeltaCoalescer(
        publisher=pub,
        correlation=_CORRELATION,
        fail_count=flag,
        coalesce_ms=60_000,
        coalesce_chars=512,
    )
    await coalescer.handle(AssistantDelta(request_id="r", delta="left over"))
    assert pub.events == []
    await coalescer.aclose()
    assert len(pub.events) == 1
    wrapped = pub.events[0]
    assert isinstance(wrapped, SubagentEvent)
    assert isinstance(wrapped.inner, AssistantDelta)
    assert wrapped.inner.delta == "left over"
