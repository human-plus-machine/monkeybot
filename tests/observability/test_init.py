"""Tests for observability lifecycle."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from monkeybot.observability import (
    get_tracer,
    init_observability,
    is_observability_enabled,
    shutdown_observability,
)


def test_init_observability_noop_when_master_disabled(
    monkeypatch: pytest.MonkeyPatch, reset_observability_state: None
) -> None:
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "false")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
    assert init_observability() is False
    assert is_observability_enabled() is False


def test_init_observability_noop_when_otel_missing(
    monkeypatch: pytest.MonkeyPatch, reset_observability_state: None
) -> None:
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")

    real_import = importlib.import_module

    def _fake_import(name: str, package: str | None = None) -> object:
        if name.startswith("opentelemetry"):
            raise ImportError("simulated missing otel")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", _fake_import)
    assert init_observability() is False
    assert is_observability_enabled() is False


def test_init_observability_idempotent(
    monkeypatch: pytest.MonkeyPatch, reset_observability_state: None
) -> None:
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()

    def _test_processor(exporter_kind: str) -> SimpleSpanProcessor:
        del exporter_kind
        return SimpleSpanProcessor(exporter)

    monkeypatch.setattr("monkeybot.observability._create_span_processor", _test_processor)
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
    assert init_observability() is True
    assert init_observability() is True
    assert is_observability_enabled() is True


def test_init_observability_otlp_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, reset_observability_state: None
) -> None:
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp")

    def _raise(_kind: str) -> object:
        raise RuntimeError("bad endpoint")

    monkeypatch.setattr("monkeybot.observability._create_span_processor", _raise)
    assert init_observability() is False
    assert is_observability_enabled() is False


def test_shutdown_observability_flushes_and_resets(otel_memory_exporter) -> None:
    from opentelemetry import trace

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("probe"):
        pass
    finished = otel_memory_exporter.get_finished_spans()
    assert any(s.name == "probe" for s in finished)
    shutdown_observability()
    assert is_observability_enabled() is False


def test_shutdown_observability_noop_when_never_init(reset_observability_state: None) -> None:
    shutdown_observability()


def test_get_tracer_returns_noop_when_disabled(reset_observability_state: None) -> None:
    tracer = get_tracer()
    with tracer.start_as_current_span("x") as span:
        assert span.is_recording() is False


def test_import_runtime_loop_with_observability_disabled(
    monkeypatch: pytest.MonkeyPatch, reset_observability_state: None
) -> None:
    """Loop module must import when OTel is not initialized (lazy observability imports)."""
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "false")
    import monkeybot.core.runtime.loop as loop_mod

    assert loop_mod.run is not None
    assert is_observability_enabled() is False


def test_init_observability_sqlite_enabled(
    monkeypatch: pytest.MonkeyPatch,
    reset_observability_state: None,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db_path = tmp_path / "traces.db"
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "sqlite")
    monkeypatch.setenv("MONKEYBOT_TRACES_DB", str(db_path))
    with caplog.at_level("INFO"):
        assert init_observability() is True
    assert is_observability_enabled() is True
    assert any(
        f"sqlite span exporter db={db_path}" in r.getMessage() for r in caplog.records
    )
    shutdown_observability()


def test_init_observability_sqlite_missing_db_returns_false(
    monkeypatch: pytest.MonkeyPatch, reset_observability_state: None
) -> None:
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "sqlite")
    monkeypatch.delenv("MONKEYBOT_TRACES_DB", raising=False)
    assert init_observability() is False
    assert is_observability_enabled() is False


def test_init_observability_rejects_unknown_exporter(
    monkeypatch: pytest.MonkeyPatch, reset_observability_state: None
) -> None:
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "jaeger")
    assert init_observability() is False
    assert is_observability_enabled() is False


@pytest.mark.skip(
    reason="Dev dependency group always installs opentelemetry; use optional-extra CI job to enforce"
)
def test_import_loop_without_observability_extra() -> None:
    """Placeholder for subprocess import without opentelemetry installed (spec AC-008)."""
