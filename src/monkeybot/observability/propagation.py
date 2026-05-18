"""W3C trace context propagation for subagent subprocesses."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from monkeybot.observability._state import is_observability_enabled

if TYPE_CHECKING:
    from opentelemetry.context import Context

logger = logging.getLogger(__name__)


def inject_traceparent(carrier: dict[str, str]) -> None:
    """Inject W3C traceparent (and tracestate if present) from the current context.

    No-op when observability is disabled, OTel is unavailable, or the current span
    context is invalid. Never raises.
    """
    if not is_observability_enabled():
        return
    try:
        from opentelemetry import propagate, trace
    except ImportError:
        return
    try:
        span_ctx = trace.get_current_span().get_span_context()
        if not span_ctx.is_valid:
            return
        propagate.inject(carrier)
    except Exception:
        return


def extract_traceparent(carrier: dict[str, str]) -> Context | None:
    """Extract an OTel Context from carrier keys ``traceparent`` / ``tracestate``.

    Returns None when the value is missing, malformed, or yields a non-valid span context.
    Logs a WARNING once per bad value (include no user content). Never raises.
    """
    raw = carrier.get("traceparent")
    if raw is None:
        return None
    if not isinstance(raw, str):
        logger.warning("malformed traceparent in carrier: expected string")
        return None
    traceparent = raw.strip()
    if not traceparent:
        return None
    try:
        from opentelemetry import propagate, trace
    except ImportError:
        return None
    try:
        ctx = propagate.extract(carrier)
        span_ctx = trace.get_current_span(ctx).get_span_context()
        if not span_ctx.is_valid:
            logger.warning("malformed traceparent: invalid span context after extract")
            return None
        return ctx
    except Exception:
        logger.warning("malformed traceparent: propagation extract failed")
        return None
