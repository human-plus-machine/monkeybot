"""CI contract: loop span names and required attribute keys match design catalog.

Catalog: .monkeymode/observability/design/1a-discovery.md (span hierarchy),
1b-contracts.md (helpers + attrs). Values are not asserted except usage zeros.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan

from monkeybot.core.llm.provider import Done, Message, TextDelta, ToolCall, UsageEvent
from monkeybot.core.runtime.loop import run
from monkeybot.core.types.content_blocks import Text
from tests.core.test_loop import (
    AllowInspector,
    FakeHistory,
    FakeProvider,
    RecordingExecutor,
    _ctx,
)

# Span names that must appear in the scenarios below (1a-discovery.md).
EXPECTED_SPAN_NAMES: frozenset[str] = frozenset(
    {
        "monkeybot.run",
        "monkeybot.turn",
        "monkeybot.llm.stream",
        "monkeybot.tool",
        "monkeybot.context.summarize",
    }
)

# Required attribute keys per span (keys only; 1a + 1b).
REQUIRED_ATTRS: dict[str, frozenset[str]] = {
    "monkeybot.run": frozenset(
        {"thread.id", "request.id", "user.message", "trace.input", "trace.output"}
    ),
    "monkeybot.turn": frozenset({"turn.index", "thread.id", "request.id"}),
    "monkeybot.llm.stream": frozenset(
        {
            "gen_ai.request.model",
            "gen_ai.operation.name",
            "thread.id",
            "request.id",
            "gen_ai.usage.input_tokens",
            "gen_ai.usage.output_tokens",
        }
    ),
    "monkeybot.tool": frozenset(
        {
            "tool.name",
            "tool.call_id",
            "thread.id",
            "request.id",
            "tool.input",
        }
    ),
    "monkeybot.context.summarize": frozenset(
        {"thread.id", "request.id", "turns.summarized"}
    ),
}

assert frozenset(REQUIRED_ATTRS) <= EXPECTED_SPAN_NAMES


def _attribute_keys(span: ReadableSpan) -> set[str]:
    attrs: Mapping[str, Any] | None = span.attributes
    if attrs is None:
        return set()
    return set(attrs.keys())


def _finished_spans(exporter: Any) -> list[ReadableSpan]:
    return list(exporter.get_finished_spans())


def _span_by_name(spans: list[ReadableSpan], name: str) -> ReadableSpan:
    matches = [s for s in spans if s.name == name]
    assert matches, f"no span named {name!r}; got {[s.name for s in spans]}"
    return matches[0]


def _assert_required_keys(span: ReadableSpan, required: frozenset[str]) -> None:
    keys = _attribute_keys(span)
    missing = required - keys
    assert not missing, f"{span.name} missing attribute keys: {sorted(missing)}"


def _assert_tool_outcome(span: ReadableSpan) -> None:
    keys = _attribute_keys(span)
    assert "tool.output" in keys or "error" in keys, (
        f"{span.name} must have tool.output or error after execution"
    )


@pytest.mark.asyncio
async def test_span_contract_text_only_includes_run_turn_llm_keys(
    otel_memory_exporter: Any,
) -> None:
    prov = FakeProvider(
        [
            [
                TextDelta(text="hi"),
                UsageEvent(input_tokens=1, output_tokens=2, cached_tokens=3),
                Done(),
            ]
        ]
    )
    async for _ in run(
        "hello",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        pass

    spans = _finished_spans(otel_memory_exporter)
    names = {s.name for s in spans}
    for required_name in ("monkeybot.run", "monkeybot.turn", "monkeybot.llm.stream"):
        assert required_name in names

    run_span = _span_by_name(spans, "monkeybot.run")
    _assert_required_keys(run_span, REQUIRED_ATTRS["monkeybot.run"])
    assert run_span.attributes.get("thread.id") == "t1"
    assert run_span.attributes.get("request.id") == "r1"

    turn_span = _span_by_name(spans, "monkeybot.turn")
    _assert_required_keys(turn_span, REQUIRED_ATTRS["monkeybot.turn"])

    llm_span = _span_by_name(spans, "monkeybot.llm.stream")
    _assert_required_keys(llm_span, REQUIRED_ATTRS["monkeybot.llm.stream"])
    assert llm_span.attributes.get("gen_ai.usage.input_tokens") == 1
    assert llm_span.attributes.get("gen_ai.usage.output_tokens") == 2


@pytest.mark.asyncio
async def test_span_contract_tool_path_includes_tool_span_keys(
    otel_memory_exporter: Any,
) -> None:
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

    spans = _finished_spans(otel_memory_exporter)
    assert "monkeybot.tool" in {s.name for s in spans}

    tool_span = _span_by_name(spans, "monkeybot.tool")
    _assert_required_keys(tool_span, REQUIRED_ATTRS["monkeybot.tool"])
    _assert_tool_outcome(tool_span)
    assert tool_span.attributes.get("tool.name") == "run_command"
    assert tool_span.attributes.get("tool.call_id") == "c1"


@pytest.mark.asyncio
async def test_span_contract_summarize_path_includes_summarize_span_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    otel_memory_exporter: Any,
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

    spans = _finished_spans(otel_memory_exporter)
    assert "monkeybot.context.summarize" in {s.name for s in spans}

    summarize_span = _span_by_name(spans, "monkeybot.context.summarize")
    _assert_required_keys(summarize_span, REQUIRED_ATTRS["monkeybot.context.summarize"])
    assert summarize_span.attributes.get("thread.id") == "t1"
    assert summarize_span.attributes.get("request.id") == "r1"
    assert summarize_span.attributes.get("turns.summarized") is not None


@pytest.mark.asyncio
async def test_span_contract_llm_usage_keys_when_usage_event_missing(
    otel_memory_exporter: Any,
) -> None:
    prov = FakeProvider([[TextDelta(text="reply"), Done()]])
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

    spans = _finished_spans(otel_memory_exporter)
    llm_span = _span_by_name(spans, "monkeybot.llm.stream")
    keys = _attribute_keys(llm_span)
    assert "gen_ai.usage.input_tokens" in keys
    assert "gen_ai.usage.output_tokens" in keys
    assert llm_span.attributes.get("gen_ai.usage.input_tokens") == 0
    assert llm_span.attributes.get("gen_ai.usage.output_tokens") == 0
    assert "gen_ai.usage.total_tokens" not in keys
