"""FastAPI OpenTelemetry instrumentation (Story 4)."""

from __future__ import annotations

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from monkeybot.observability.instrumentation import instrument_fastapi_app


def _reset_otel_globals() -> None:
    from monkeybot.observability import _state

    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    _state._initialized = False
    _state._enabled = False


@pytest.mark.asyncio
async def test_instrument_fastapi_emits_http_span_on_minimal_app(
    otel_memory_exporter: InMemorySpanExporter,
) -> None:
    """HTTP server spans export when FastAPI is instrumented with an active SDK provider."""
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    instrument_fastapi_app(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200

    trace.get_tracer_provider().force_flush()  # type: ignore[union-attr]
    http_spans = [
        s
        for s in otel_memory_exporter.get_finished_spans()
        if s.attributes
        and (
            "http.method" in s.attributes
            or "http.route" in s.attributes
            or "http.target" in s.attributes
        )
    ]
    assert http_spans


@pytest.mark.asyncio
async def test_gateway_startup_instruments_fastapi_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from monkeybot.core.mcp.mcp_client import MCPClient
    from monkeybot.gateway.sse.app import app
    from monkeybot.observability import is_observability_enabled, shutdown_observability

    exporter = InMemorySpanExporter()

    def _memory_processor(_kind: str) -> SimpleSpanProcessor:
        return SimpleSpanProcessor(exporter)

    shutdown_observability()
    _reset_otel_globals()
    monkeypatch.setattr("monkeybot.observability._create_span_processor", _memory_processor)

    async def _skip_mcp_load(self: MCPClient, _path: object, *_a: object, **_kw: object) -> None:
        return

    monkeypatch.setattr(MCPClient, "load_from_config", _skip_mcp_load)
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
    monkeypatch.setenv("DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("MCP_CONFIG", "/nonexistent/mcp.json")

    async with LifespanManager(app):
        assert is_observability_enabled()
        assert getattr(app, "_monkeybot_otel_fastapi_instrumented", False)

    shutdown_observability()
    _reset_otel_globals()
