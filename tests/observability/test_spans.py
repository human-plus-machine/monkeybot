"""Tests for span helpers."""

from __future__ import annotations

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.observability.spans import (
    set_span_attribute_safe,
    span_llm,
    span_run,
    span_tool,
)


def _ctx() -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[ToolDef("run_command", "Run shell", {})],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
        summarization_model=None,
        workspace_root=None,
        context_window_tokens=200_000,
    )


@pytest.mark.asyncio
async def test_set_span_attribute_safe_strips_denylisted_key(otel_memory_exporter) -> None:
    from opentelemetry import trace

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("x") as span:
        set_span_attribute_safe(span, "my_api_key", "secret-value")
        attrs = dict(span.attributes or {})
    assert "my_api_key" not in attrs


@pytest.mark.asyncio
async def test_span_run_sets_root_attributes(otel_memory_exporter) -> None:
    ctx = _ctx()
    async with span_run(ctx, user_message="hello"):
        pass
    spans = otel_memory_exporter.get_finished_spans()
    run = next(s for s in spans if s.name == "monkeybot.run")
    assert run.attributes["thread.id"] == "t1"
    assert run.attributes["request.id"] == "r1"
    assert run.attributes["trace.input"] == "hello"
    assert run.attributes["user.message"] == "hello"


@pytest.mark.asyncio
async def test_span_hierarchy_run_turn_llm(otel_memory_exporter) -> None:
    from monkeybot.observability.spans import begin_turn_span, end_turn_span

    ctx = _ctx()
    async with span_run(ctx, user_message="hi"):
        handle = begin_turn_span(turn_index=1, thread_id="t1", request_id="r1")
        try:
            async with span_llm(ctx=ctx):
                pass
        finally:
            end_turn_span(handle)
    spans = otel_memory_exporter.get_finished_spans()
    run = next(s for s in spans if s.name == "monkeybot.run")
    turn = next(s for s in spans if s.name == "monkeybot.turn")
    llm = next(s for s in spans if s.name == "monkeybot.llm.stream")
    assert run.context.trace_id == turn.context.trace_id == llm.context.trace_id
    assert turn.parent is not None
    assert llm.parent is not None
    assert format(turn.parent.span_id, "016x") == format(run.context.span_id, "016x")
    assert format(llm.parent.span_id, "016x") == format(turn.context.span_id, "016x")


@pytest.mark.asyncio
async def test_span_llm_sets_gen_ai_attributes(otel_memory_exporter) -> None:
    from monkeybot.observability.spans import set_llm_usage

    ctx = _ctx()
    async with span_llm(ctx=ctx):
        set_llm_usage(input_tokens=3, output_tokens=4, cached_tokens=1)
    llm = next(
        s for s in otel_memory_exporter.get_finished_spans() if s.name == "monkeybot.llm.stream"
    )
    assert llm.attributes["gen_ai.request.model"] == "gemini-2.5-flash"
    assert llm.attributes["gen_ai.operation.name"] == "chat"
    assert llm.attributes["gen_ai.usage.input_tokens"] == 3
    assert llm.attributes["gen_ai.usage.output_tokens"] == 4


@pytest.mark.asyncio
async def test_span_tool_truncates_input_output(otel_memory_exporter) -> None:
    from monkeybot.observability.spans import record_tool_outcome

    big = "x" * 20_000
    async with span_tool(
        tool_name="run_command",
        tool_call_id="c1",
        thread_id="t1",
        request_id="r1",
        args={"q": big},
    ):
        record_tool_outcome("ok" * 20_000, None)
    tool_spans = [s for s in otel_memory_exporter.get_finished_spans() if s.name == "monkeybot.tool"]
    assert tool_spans
    tool = tool_spans[0]
    assert str(tool.attributes["tool.input"]).endswith("…[truncated]")
    assert str(tool.attributes["tool.output"]).endswith("…[truncated]")


@pytest.mark.asyncio
async def test_span_tool_error_sets_error_attr(otel_memory_exporter) -> None:
    with pytest.raises(RuntimeError):
        async with span_tool(
            tool_name="run_command",
            tool_call_id="c1",
            thread_id="t1",
            request_id="r1",
        ):
            raise RuntimeError("boom")
    tool_spans = [s for s in otel_memory_exporter.get_finished_spans() if s.name == "monkeybot.tool"]
    assert tool_spans
    assert "error" in tool_spans[0].attributes


@pytest.mark.asyncio
async def test_spans_noop_when_disabled(reset_observability_state: None, otel_memory_exporter) -> None:
    from monkeybot.observability import shutdown_observability

    before = len(otel_memory_exporter.get_finished_spans())
    shutdown_observability()
    ctx = _ctx()
    async with span_run(ctx, user_message="hello"), span_llm(ctx=ctx):
        pass
    assert len(otel_memory_exporter.get_finished_spans()) == before
