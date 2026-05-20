"""Integration tests for loop OpenTelemetry spans."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.llm.provider import (
    Done,
    Message,
    TextDelta,
    ToolCall,
)
from monkeybot.core.runtime.loop import _provider_messages_prompt_summary, run
from monkeybot.core.types.content_blocks import Text, ToolResponse
from tests.core.test_loop import (
    AllowInspector,
    FakeHistory,
    FakeProvider,
    RecordingExecutor,
    _ctx,
)


@pytest.mark.asyncio
async def test_loop_no_spans_when_observability_disabled(
    monkeypatch: pytest.MonkeyPatch,
    otel_memory_exporter: object,
) -> None:
    from monkeybot.observability import shutdown_observability

    shutdown_observability()
    monkeypatch.delenv("MONKEYBOT_OTEL_ENABLED", raising=False)
    before = len(otel_memory_exporter.get_finished_spans())  # type: ignore[attr-defined]
    prov = FakeProvider([[TextDelta(text="hi"), Done()]])
    async for _ in run(
        "hello",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=2,
    ):
        pass
    after = len(otel_memory_exporter.get_finished_spans())  # type: ignore[attr-defined]
    assert before == after


@pytest.mark.asyncio
async def test_loop_simple_reply_span_tree(otel_memory_exporter) -> None:
    prov = FakeProvider([[TextDelta(text="hello"), Done()]])
    async for _ in run(
        "user msg",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=2,
    ):
        pass
    spans = otel_memory_exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert "monkeybot.run" in names
    assert "monkeybot.turn" in names
    assert "monkeybot.llm.stream" in names
    run_span = next(s for s in spans if s.name == "monkeybot.run")
    turn_span = next(s for s in spans if s.name == "monkeybot.turn")
    llm = next(s for s in spans if s.name == "monkeybot.llm.stream")
    assert run_span.context.trace_id == turn_span.context.trace_id == llm.context.trace_id
    assert turn_span.parent is not None
    assert llm.parent is not None
    assert format(turn_span.parent.span_id, "016x") == format(run_span.context.span_id, "016x")
    assert format(llm.parent.span_id, "016x") == format(turn_span.context.span_id, "016x")
    assert run_span.attributes.get("gen_ai.request.model") is None
    assert llm.attributes.get("gen_ai.request.model") == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_loop_tool_span_on_execute(otel_memory_exporter) -> None:
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
    tool = next(
        (s for s in otel_memory_exporter.get_finished_spans() if s.name == "monkeybot.tool"),
        None,
    )
    assert tool is not None
    assert tool.attributes.get("tool.name") == "run_command"
    assert tool.attributes.get("tool.input")


@pytest.mark.asyncio
async def test_loop_summarize_span_when_over_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, otel_memory_exporter
) -> None:
    monkeypatch.delenv("CONTEXT_SUMMARIZATION_MODEL", raising=False)
    preload = [
        Message(
            role="user" if i % 2 == 0 else "assistant",
            content=[Text(text="z" * 600)],
        )
        for i in range(8)
    ]
    prov = FakeProvider(
        [
            [TextDelta(text=" compressed summary "), Done()],
            [TextDelta(text="final"), Done()],
        ]
    )
    ctx = _ctx(workspace_root=tmp_path, context_window_tokens=800)
    async for _ in run(
        "hello",
        ctx,
        provider=prov,
        history=FakeHistory(preload),
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=4,
    ):
        pass
    summarize = next(
        (s for s in otel_memory_exporter.get_finished_spans() if s.name == "monkeybot.context.summarize"),
        None,
    )
    assert summarize is not None
    assert summarize.attributes.get("turns.summarized") is not None


@pytest.mark.asyncio
async def test_loop_trace_io_on_run_span(otel_memory_exporter) -> None:
    prov = FakeProvider([[TextDelta(text="assistant reply"), Done()]])
    async for _ in run(
        "user input",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=2,
    ):
        pass
    run_span = next(s for s in otel_memory_exporter.get_finished_spans() if s.name == "monkeybot.run")
    assert run_span.attributes.get("trace.input") == "user input"
    assert run_span.attributes.get("trace.output") == "assistant reply"


@pytest.mark.asyncio
async def test_loop_long_user_message_truncated_on_span(otel_memory_exporter) -> None:
    long_msg = "x" * 9000
    prov = FakeProvider([[TextDelta(text="ok"), Done()]])
    async for _ in run(
        long_msg,
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=2,
    ):
        pass
    run_span = next(s for s in otel_memory_exporter.get_finished_spans() if s.name == "monkeybot.run")
    assert str(run_span.attributes.get("trace.input", "")).endswith("…[truncated]")


def test_provider_messages_prompt_summary_tool_response_uses_tool_name() -> None:
    messages = [
        Message(
            role="user",
            content=[
                ToolResponse(
                    id="c1",
                    tool_name="run_command",
                    result=[Text(text="ok")],
                ),
            ],
        ),
    ]
    summary = _provider_messages_prompt_summary(messages)
    assert "[tool_result run_command]" in summary
