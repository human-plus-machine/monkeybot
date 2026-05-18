"""Tests for observability.instrumentation (Story 4)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from fastapi import FastAPI
from opentelemetry import trace

from monkeybot.core.llm.provider import Done, Message, ProviderEvent, TextDelta, ToolDef
from monkeybot.observability import shutdown_observability
from monkeybot.observability.instrumentation import (
    ObservingProvider,
    add_tool_hook_span_event,
    instrument_fastapi_app,
)


class _FakeInner:
    name = "fake"

    @property
    def supports_streaming(self) -> bool:
        return True

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools, model

        async def _gen() -> AsyncIterator[ProviderEvent]:
            yield TextDelta(text="hi")
            yield Done()

        return _gen()

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
    ) -> int:
        del messages, tools, model
        return 42


@pytest.mark.asyncio
async def test_observing_provider_stream_emits_llm_span_when_enabled(
    otel_memory_exporter: Any,
) -> None:
    wrapped = ObservingProvider(_FakeInner())
    async for _ in wrapped.stream([], [], model="gemini-2.5-flash"):
        pass

    names = {s.name for s in otel_memory_exporter.get_finished_spans()}
    assert "monkeybot.llm.stream" in names


@pytest.mark.asyncio
async def test_observing_provider_noop_when_observability_disabled(
    otel_memory_exporter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MONKEYBOT_OTEL_ENABLED", raising=False)
    shutdown_observability()
    wrapped = ObservingProvider(_FakeInner())
    events: list[ProviderEvent] = []
    async for evt in wrapped.stream([], [], model="gemini-2.5-flash"):
        events.append(evt)
    assert len(events) == 2
    assert not otel_memory_exporter.get_finished_spans()


@pytest.mark.asyncio
async def test_observing_provider_delegates_count_input_tokens() -> None:
    wrapped = ObservingProvider(_FakeInner())
    n = await wrapped.count_input_tokens([], [], model="m")
    assert n == 42


def test_instrument_fastapi_app_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MONKEYBOT_OTEL_ENABLED", raising=False)
    shutdown_observability()
    app = FastAPI()
    instrument_fastapi_app(app)
    assert getattr(app, "_monkeybot_otel_fastapi_instrumented", False) is False


def test_add_tool_hook_span_event_adds_event_on_current_span(
    otel_memory_exporter: Any,
) -> None:
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("monkeybot.tool") as span:
        add_tool_hook_span_event(phase="pre_tool", tool_name="run_command")
        span.end()

    finished = otel_memory_exporter.get_finished_spans()
    assert finished
    events = finished[0].events
    assert any(e.name == "monkeybot.hook.pre_tool" for e in events)
    hook_evt = next(e for e in events if e.name == "monkeybot.hook.pre_tool")
    assert hook_evt.attributes == {"tool.name": "run_command"}


def test_add_tool_hook_span_event_noop_when_not_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MONKEYBOT_OTEL_ENABLED", raising=False)
    shutdown_observability()
    add_tool_hook_span_event(phase="pre_tool", tool_name="run_command")
