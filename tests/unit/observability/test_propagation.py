"""Unit tests for W3C trace context propagation helpers."""

from __future__ import annotations

import logging
import re

import pytest
from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from monkeybot.observability.propagation import extract_traceparent, inject_traceparent

_W3C_TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-0[1-9a-f]$")


@pytest.fixture(autouse=True)
def _w3c_textmap() -> None:
    propagate.set_global_textmap(TraceContextTextMapPropagator())


def test_inject_traceparent_writes_headers_when_span_active(otel_memory_exporter) -> None:
    tracer = trace.get_tracer("test")
    carrier: dict[str, str] = {}
    with tracer.start_as_current_span("active"):
        inject_traceparent(carrier)
    assert "traceparent" in carrier
    assert _W3C_TRACEPARENT.match(carrier["traceparent"])


def test_inject_traceparent_noop_when_observability_disabled(
    monkeypatch: pytest.MonkeyPatch,
    reset_observability_state: None,
) -> None:
    from monkeybot.observability import _state

    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "false")
    _state._initialized = True
    _state._enabled = False
    carrier: dict[str, str] = {"preset": "keep"}
    inject_traceparent(carrier)
    assert carrier == {"preset": "keep"}


def test_inject_traceparent_noop_without_valid_current_span(otel_memory_exporter) -> None:
    carrier: dict[str, str] = {}
    inject_traceparent(carrier)
    assert carrier == {}


def test_extract_traceparent_round_trips_same_trace_id(otel_memory_exporter) -> None:
    tracer = trace.get_tracer("test")
    carrier_in: dict[str, str] = {}
    parent_trace_id: int
    with tracer.start_as_current_span("parent") as parent_span:
        inject_traceparent(carrier_in)
        parent_trace_id = parent_span.get_span_context().trace_id

    extracted = extract_traceparent(carrier_in)
    assert extracted is not None

    token = otel_context.attach(extracted)
    try:
        with tracer.start_as_current_span("child") as child_span:
            assert child_span.get_span_context().trace_id == parent_trace_id
    finally:
        otel_context.detach(token)

    finished = otel_memory_exporter.get_finished_spans()
    trace_ids = {s.context.trace_id for s in finished}
    assert len(trace_ids) == 1
    assert parent_trace_id in trace_ids
    span_ids = {s.context.span_id for s in finished}
    assert len(span_ids) == 2


def test_extract_traceparent_returns_none_when_missing() -> None:
    assert extract_traceparent({}) is None


def test_extract_traceparent_returns_none_when_malformed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    assert extract_traceparent({"traceparent": "invalid"}) is None
    warnings = [r.message.lower() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings
    assert any("traceparent" in msg or "malformed" in msg or "propagat" in msg for msg in warnings)
