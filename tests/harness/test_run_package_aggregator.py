"""RunPackageAggregatorMW routes bus events into the active accumulator frame."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.core.harness.event_bus import EventBus
from src.core.harness.events import EventKind, HarnessEvent, Principal, VersionTriple
from src.core.harness.middleware.run_package_aggregator import RunPackageAggregatorMW
from src.core.harness.run_package_accumulator import RunPackageAccumulator


@pytest.mark.asyncio
async def test_aggregator_fills_token_and_tool_rows() -> None:
    acc = RunPackageAccumulator()
    bus = EventBus(include_default_logger=False)
    kinds = frozenset(
        {
            EventKind.LLM_RESULT,
            EventKind.TOOL_CALL,
            EventKind.TOOL_RESULT,
        }
    )
    bus.subscribe(RunPackageAggregatorMW(acc), kinds=kinds, timeout_s=1.0)
    p = Principal(kind="user", id="u")
    v = VersionTriple(harness="1", deep_agents="0.1", model="m")
    now = datetime.now(UTC)
    acc.begin_root("run_1", "sess", p, v, now, [])
    await bus.publish(
        HarnessEvent(
            run_id="run_1",
            session_id="sess",
            principal=p,
            versions=v,
            ts=now,
            kind=EventKind.TOOL_CALL,
            payload={"call_id": "t1", "name": "grep", "args_redacted": {"pattern": "x"}},
        )
    )
    await bus.publish(
        HarnessEvent(
            run_id="run_1",
            session_id="sess",
            principal=p,
            versions=v,
            ts=now,
            kind=EventKind.TOOL_RESULT,
            payload={"call_id": "t1", "result_summary": "ok", "latency_ms": 12, "success": True},
        )
    )
    await bus.publish(
        HarnessEvent(
            run_id="run_1",
            session_id="sess",
            principal=p,
            versions=v,
            ts=now,
            kind=EventKind.LLM_RESULT,
            payload={
                "call_id": "l1",
                "model": "gemini",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "latency_ms": 100,
            },
        )
    )
    ended = datetime.now(UTC)
    pkg = acc.complete_root([], "pass", ended)
    assert len(pkg.tool_calls) == 1
    assert pkg.tool_calls[0].name == "grep"
    assert len(pkg.token_trace) == 1
    assert pkg.token_trace[0].total_tokens == 15
