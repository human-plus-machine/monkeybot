"""OpenTelemetry tracing for monkeybot (optional ``[observability]`` extra).

Master switch: ``MONKEYBOT_OTEL_ENABLED`` must be truthy to opt in. When unset,
tracing stays disabled even if OTel packages are installed (explicit rollout).
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import os
from typing import Any

from monkeybot.observability import _state
from monkeybot.observability.instrumentation import (
    ObservingProvider,
    add_tool_hook_span_event,
    get_current_trace_id_hex_optional,
    instrument_fastapi_app,
)
from monkeybot.observability.propagation import extract_traceparent, inject_traceparent
from monkeybot.observability.spans import (
    is_denied_attribute_key,
    set_llm_io,
    set_llm_usage,
    set_run_output,
    set_summarize_turns,
    set_turn_io,
    set_turn_prompt_tokens,
    span_llm,
    span_run,
    span_subagent,
    span_summarize,
    span_tool,
    span_turn,
    truncate,
)

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_truthy(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


def _env_sdk_disabled() -> bool:
    return os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() in _TRUTHY


def is_observability_enabled() -> bool:
    return _state.is_observability_enabled()


def _create_span_processor(exporter: str) -> Any:
    """Build a span processor for ``otlp``, ``console``, or ``sqlite``. Tests may monkeypatch."""
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return BatchSpanProcessor(OTLPSpanExporter())
    if exporter == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return BatchSpanProcessor(ConsoleSpanExporter())
    if exporter == "sqlite":
        from monkeybot.observability.sqlite_exporter import (
            SqliteSpanExporter,
            load_sqlite_exporter_config,
        )

        config = load_sqlite_exporter_config()
        if config is None:
            logger.info(
                "observability disabled (OTEL_TRACES_EXPORTER=sqlite requires MONKEYBOT_TRACES_DB)"
            )
            return None
        logger.info("sqlite span exporter db=%s", config.db_path)
        return BatchSpanProcessor(
            SqliteSpanExporter(config),
            schedule_delay_millis=500,
            max_export_batch_size=64,
        )
    return None


def init_observability() -> bool:
    if _state._initialized:
        return _state._enabled

    _state._initialized = True
    _state._enabled = False

    if not _env_truthy("MONKEYBOT_OTEL_ENABLED"):
        logger.info("observability disabled (MONKEYBOT_OTEL_ENABLED is not set or false)")
        return False

    if _env_sdk_disabled():
        logger.info("observability disabled (OTEL_SDK_DISABLED=true)")
        return False

    try:
        trace_api = importlib.import_module("opentelemetry.trace")
        resources = importlib.import_module("opentelemetry.sdk.resources")
        trace_sdk = importlib.import_module("opentelemetry.sdk.trace")
        w3c = importlib.import_module("opentelemetry.trace.propagation.tracecontext")
        propagate = importlib.import_module("opentelemetry.propagate")
    except ImportError:
        logger.warning("observability disabled (OpenTelemetry packages not installed)")
        return False

    exporter = os.environ.get("OTEL_TRACES_EXPORTER", "").strip().lower()
    if exporter not in {"otlp", "console", "sqlite"}:
        logger.info(
            "observability disabled (OTEL_TRACES_EXPORTER must be otlp, console, or sqlite, got %r)",
            exporter or "<unset>",
        )
        return False

    service_name = os.environ.get("OTEL_SERVICE_NAME", "monkeybot")
    attrs: dict[str, str] = {"service.name": service_name}
    try:
        version = importlib.metadata.version("monkeybot")
    except importlib.metadata.PackageNotFoundError:
        version = None
    if version:
        attrs["service.version"] = version

    try:
        processor = _create_span_processor(exporter)
        if processor is None:
            return False
        resource = resources.Resource.create(attrs)
        provider = trace_sdk.TracerProvider(resource=resource)
        provider.add_span_processor(processor)
        trace_api.set_tracer_provider(provider)
        propagate.set_global_textmap(w3c.TraceContextTextMapPropagator())
        _state._enabled = True
        logger.info("observability enabled (exporter=%s, service=%s)", exporter, service_name)
        return True
    except Exception as exc:
        logger.warning("observability init failed: %s", exc)
        return False


def shutdown_observability() -> None:
    if not _state._initialized:
        return
    try:
        if _state._enabled:
            trace_api = importlib.import_module("opentelemetry.trace")
            provider = trace_api.get_tracer_provider()
            shutdown = getattr(provider, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown(timeout_millis=5000)
                except TypeError:
                    shutdown()
    except Exception as exc:
        logger.warning("observability shutdown failed: %s", exc)
    finally:
        _state._initialized = False
        _state._enabled = False


class _NoOpTracer:
    def start_span(self, *_a: Any, **_k: Any) -> _NoOpSpan:
        return _NoOpSpan()

    def start_as_current_span(self, *_a: Any, **_k: Any) -> _NoOpSpan:
        return _NoOpSpan()


class _NoOpSpan:
    def __enter__(self) -> _NoOpSpan:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def set_attribute(self, *_a: Any, **_k: Any) -> None:
        return None

    def record_exception(self, *_a: Any, **_k: Any) -> None:
        return None

    def set_status(self, *_a: Any, **_k: Any) -> None:
        return None

    def end(self) -> None:
        return None

    def is_recording(self) -> bool:
        return False


def get_tracer(name: str = "monkeybot") -> Any:
    if not _state._enabled:
        try:
            from opentelemetry.trace import NoOpTracer

            return NoOpTracer()
        except ImportError:
            return _NoOpTracer()
    trace_api = importlib.import_module("opentelemetry.trace")
    return trace_api.get_tracer(name)


__all__ = [
    "ObservingProvider",
    "add_tool_hook_span_event",
    "extract_traceparent",
    "get_current_trace_id_hex_optional",
    "instrument_fastapi_app",
    "get_tracer",
    "init_observability",
    "inject_traceparent",
    "is_denied_attribute_key",
    "is_observability_enabled",
    "set_llm_io",
    "set_llm_usage",
    "set_run_output",
    "set_summarize_turns",
    "set_turn_io",
    "set_turn_prompt_tokens",
    "shutdown_observability",
    "span_llm",
    "span_run",
    "span_subagent",
    "span_summarize",
    "span_tool",
    "span_turn",
    "truncate",
]
