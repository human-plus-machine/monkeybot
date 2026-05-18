"""Hook span events on monkeybot.tool (Story 4)."""

from __future__ import annotations

from typing import Any

import pytest

from monkeybot.core.llm.provider import Done, TextDelta, ToolCall
from monkeybot.core.runtime.loop import run
from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider, RecordingExecutor, _ctx


@pytest.mark.asyncio
async def test_pre_post_tool_hook_events_on_tool_span(otel_memory_exporter: Any) -> None:
    prov = FakeProvider(
        [
            [ToolCall(call_id="c1", name="run_command", args={"cmd": "echo hi"})],
            [TextDelta(text="done"), Done()],
        ]
    )
    async for _ in run(
        "go",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=4,
    ):
        pass

    tool_spans = [s for s in otel_memory_exporter.get_finished_spans() if s.name == "monkeybot.tool"]
    assert len(tool_spans) == 1
    tool = tool_spans[0]
    event_names = {e.name for e in tool.events}
    assert "monkeybot.hook.pre_tool" in event_names
    assert "monkeybot.hook.post_tool" in event_names
    for e in tool.events:
        if e.name.startswith("monkeybot.hook."):
            assert e.attributes == {"tool.name": "run_command"}

    hook_root_spans = [
        s for s in otel_memory_exporter.get_finished_spans() if s.name.startswith("monkeybot.hook")
    ]
    assert not hook_root_spans


@pytest.mark.asyncio
async def test_hook_span_events_noop_when_tracing_off(
    monkeypatch: pytest.MonkeyPatch,
    otel_memory_exporter: Any,
) -> None:
    from monkeybot.observability import shutdown_observability

    shutdown_observability()
    monkeypatch.delenv("MONKEYBOT_OTEL_ENABLED", raising=False)
    prov = FakeProvider(
        [
            [ToolCall(call_id="c1", name="run_command", args={"cmd": "echo hi"})],
            [TextDelta(text="done"), Done()],
        ]
    )
    async for _ in run(
        "go",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=4,
    ):
        pass
    assert not otel_memory_exporter.get_finished_spans()
