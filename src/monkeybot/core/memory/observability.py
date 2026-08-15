"""Safe memory spans and structured logs (no verbatim content)."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("monkeybot.memory")

_SAFE_ATTRS = frozenset(
    {
        "memory.backend",
        "memory.embedding_model",
        "memory.operation",
        "memory.role",
        "memory.wing",
        "memory.batch_size",
        "memory.drawer_count",
        "memory.result_count",
        "memory.attempt",
        "memory.status",
        "memory.error_class",
        "memory.lock_contended",
        "memory.cli",
        "memory.cli.subcommand",
    }
)


def _tracer() -> Any | None:
    try:
        from monkeybot.observability import get_tracer
        from monkeybot.observability._state import is_observability_enabled

        if not is_observability_enabled():
            return None
        return get_tracer()
    except Exception:
        logger.debug("memory tracer unavailable", exc_info=True)
        return None


def current_traceparent() -> str | None:
    try:
        from monkeybot.observability.propagation import inject_traceparent

        carrier: dict[str, str] = {}
        inject_traceparent(carrier)
        return carrier.get("traceparent")
    except Exception:
        logger.debug("memory traceparent unavailable", exc_info=True)
        return None


def current_trace_id() -> str | None:
    try:
        from monkeybot.observability.instrumentation import get_current_trace_id_hex_optional

        return get_current_trace_id_hex_optional()
    except Exception:
        logger.debug("memory trace id unavailable", exc_info=True)
        return None


def _set_attrs(span: Any, attrs: dict[str, Any]) -> None:
    for key, value in attrs.items():
        if key not in _SAFE_ATTRS or value is None:
            continue
        try:
            span.set_attribute(key, value)
        except Exception:
            logger.debug("memory span attribute skipped key=%s", key, exc_info=True)


@contextmanager
def memory_span(name: str, **attrs: Any) -> Iterator[Any | None]:
    tracer = _tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as span:
        _set_attrs(span, attrs)
        yield span


def link_to_traceparent(span: Any | None, traceparent: str | None) -> None:
    if span is None or not traceparent:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        ctx = TraceContextTextMapPropagator().extract({"traceparent": traceparent})
        parent = trace.get_current_span(ctx)
        parent_ctx = parent.get_span_context() if parent is not None else None
        if parent_ctx is None or not parent_ctx.is_valid:
            return
        span.add_link(parent_ctx)
    except Exception:
        logger.debug("memory span link skipped", exc_info=True)


def log_event(event: str, **fields: Any) -> None:
    payload = {k: v for k, v in fields.items() if v is not None}
    trace_id = current_trace_id()
    if trace_id:
        payload["trace_id"] = trace_id
    logger.info("memory %s %s", event, payload)


class Timer:
    def __init__(self) -> None:
        self._start = time.perf_counter()

    def ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000.0
