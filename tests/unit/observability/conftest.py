"""Shared OpenTelemetry test fixtures."""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from monkeybot.observability import _state
from monkeybot.observability import shutdown_observability as _shutdown


def _reset_global_tracer_provider() -> None:
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]


@pytest.fixture
def reset_observability_state() -> None:
    _reset_global_tracer_provider()
    _state._initialized = False
    _state._enabled = False
    yield
    _shutdown()
    _reset_global_tracer_provider()
    _state._initialized = False
    _state._enabled = False


@pytest.fixture
def otel_memory_exporter(reset_observability_state: None) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _state._initialized = True
    _state._enabled = True
    yield exporter
    exporter.clear()
    _shutdown()
    _reset_global_tracer_provider()
