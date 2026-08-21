"""Phase 6 integration: observability stories 1–3 wired end-to-end."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import yaml
from httpx import ASGITransport, AsyncClient
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

import monkeybot.observability as observability_pkg
from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import Done, TextDelta, ToolCall
from monkeybot.core.runtime.events import TurnComplete, UsageTotals
from monkeybot.core.runtime.loop import run
from monkeybot.core.subagents.subagent_proto import SubagentEnvelope
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider, _ctx, _ctx_with_task
from tests.core.test_subagent_trace_propagation import (
    _mem_sub,
    _NoMCP,
)


def _tier_policy_yaml() -> str:
    return "deny_patterns: []\n"


def _reset_otel_globals() -> None:
    from monkeybot.observability import _state

    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    _state._initialized = False
    _state._enabled = False


@pytest.fixture(autouse=True)
def _w3c_textmap() -> None:
    propagate.set_global_textmap(TraceContextTextMapPropagator())


@pytest.mark.integration
def test_observability_public_api_exports_stories_1_through_3() -> None:
    """Story 1 init/spans, Story 2 propagation, Story 3 contract helpers share one package."""
    required = {
        "init_observability",
        "shutdown_observability",
        "is_observability_enabled",
        "get_tracer",
        "inject_traceparent",
        "extract_traceparent",
        "span_run",
        "span_turn",
        "span_llm",
        "span_tool",
        "span_subagent",
        "span_summarize",
        "truncate",
    }
    missing = required - set(observability_pkg.__all__)
    assert not missing, f"missing from monkeybot.observability __all__: {sorted(missing)}"


@pytest.mark.integration
def test_otel_collector_example_yaml_is_valid() -> None:
    path = Path(__file__).resolve().parents[2] / "docs" / "otel-collector.example.yaml"
    raw = path.read_text(encoding="utf-8")
    config = yaml.safe_load(raw)
    assert isinstance(config, dict)
    assert "receivers" in config
    assert "exporters" in config
    assert "service" in config


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gateway_lifespan_wires_observability_with_memory_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from asgi_lifespan import LifespanManager

    from monkeybot.core.mcp.mcp_client import MCPClient
    from monkeybot.gateway.sse.app import app
    from monkeybot.observability import is_observability_enabled, shutdown_observability

    exporter = InMemorySpanExporter()

    def _memory_processor(_kind: str) -> SimpleSpanProcessor:
        return SimpleSpanProcessor(exporter)

    async def _skip_mcp_load(self: MCPClient, _path: object, *_a: object, **_kw: object) -> None:
        return

    shutdown_observability()
    _reset_otel_globals()
    monkeypatch.setattr("monkeybot.observability._create_span_processor", _memory_processor)
    monkeypatch.setattr(MCPClient, "load_from_config", _skip_mcp_load)
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
    monkeypatch.setenv("DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("MCP_CONFIG", "/nonexistent/mcp.json")

    async with LifespanManager(app):
        assert is_observability_enabled() is True

    shutdown_observability()


@pytest_asyncio.fixture
async def gateway_client_otel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, InMemorySpanExporter]]:
    """ASGI client with OTel enabled and in-memory span export."""
    exporter = InMemorySpanExporter()

    def _memory_processor(_kind: str) -> SimpleSpanProcessor:
        return SimpleSpanProcessor(exporter)

    from monkeybot.observability import shutdown_observability

    shutdown_observability()
    _reset_otel_globals()
    monkeypatch.setattr("monkeybot.observability._create_span_processor", _memory_processor)
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "none")

    db_file = tmp_path / "mb.db"
    monkeypatch.setenv("DB_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "no_mcp.json"))
    policy_file = tmp_path / "command_allowlist.yaml"
    policy_file.write_text(_tier_policy_yaml(), encoding="utf-8")
    monkeypatch.setenv("COMMAND_ALLOWLIST_CONFIG", str(policy_file))
    agent = tmp_path / "AGENT.md"
    agent.write_text("# Test agent\nYou are a test assistant.\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_MD", str(agent))
    memory = tmp_path / "memory"
    memory.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("MEMORY_PATH", str(memory))
    monkeypatch.setenv("SKILLS_PATH", str(skills))

    from asgi_lifespan import LifespanManager

    from monkeybot.core.mcp.mcp_client import MCPClient
    from monkeybot.gateway.sse.app import app

    async def _skip_mcp_load(self: MCPClient, _path: object, *_a: object, **_kw: object) -> None:
        return

    monkeypatch.setattr(MCPClient, "load_from_config", _skip_mcp_load)

    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, exporter

    shutdown_observability()
    _reset_otel_globals()
    exporter.clear()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gateway_e2e_simple_reply_emits_nested_spans(
    gateway_client_otel: tuple[AsyncClient, InMemorySpanExporter],
) -> None:
    """Gateway startup (Story 1) + loop run (Story 1) on one chat turn."""
    client, exporter = gateway_client_otel
    from monkeybot.gateway.sse.app import app

    r1 = await client.post("/sessions", json={})
    assert r1.status_code == 201
    sid = r1.json()["session_id"]
    bus = app.state.registry.get(sid)
    assert bus is not None
    _replay, q = await bus.subscribe(None)

    r2 = await client.post(
        f"/sessions/{sid}/reply",
        json={"request_id": "req-otel-e2e", "message": "Hello"},
    )
    assert r2.status_code == 200

    joined = ""
    deadline = asyncio.get_running_loop().time() + 10.0
    while '"type":"TurnComplete"' not in joined and asyncio.get_running_loop().time() < deadline:
        joined += await asyncio.wait_for(q.get(), timeout=1.0)

    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert "monkeybot.run" in names
    assert "monkeybot.turn" in names
    assert "monkeybot.llm.stream" in names
    run_span = next(s for s in spans if s.name == "monkeybot.run")
    turn_span = next(s for s in spans if s.name == "monkeybot.turn")
    assert run_span.context.trace_id == turn_span.context.trace_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integrated_run_task_subagent_shares_parent_trace_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    otel_memory_exporter: Any,
) -> None:
    """Story 1 loop spans + Story 2 inject/extract across tool task → subagent worker."""
    from monkeybot.core.subagents import subagent_worker

    mem_root = tmp_path / "mem"
    mem_root.mkdir()

    async def fake_spawn(
        script: str,
        envelope: SubagentEnvelope,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
    ):
        del script, scratch_dir, subprocess_exec, on_event
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        skills = tmp_path / "skills"
        skills.mkdir(exist_ok=True)
        monkeypatch.setenv("MONKEYBOT_SUBAGENT_WORKSPACE", str(ws))
        monkeypatch.setenv("MEMORY_STORAGE_URI", envelope.memory_storage_uri)
        monkeypatch.setenv("MONKEYBOT_SUBAGENT_SKILLS_PATH", str(skills))
        monkeypatch.setenv("MODEL_PROVIDER", "fake")
        monkeypatch.setenv(
            "MONKEYBOT_FAKE_PROVIDER_EVENTS",
            json.dumps([[{"kind": "text_delta", "text": "ok"}, {"kind": "done"}]]),
        )
        monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "true")
        monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
        monkeypatch.setattr(sys, "stdin", io.StringIO(envelope.to_json()))

        backend = MagicMock()
        backend.open = AsyncMock()
        backend.close = AsyncMock()
        backend.history.return_value = MagicMock()
        monkeypatch.setattr(subagent_worker, "create_storage_backend", lambda _url, **_kw: backend)

        mcp = MagicMock()
        mcp.load_from_config = AsyncMock()
        mcp.disconnect = AsyncMock()
        mcp._servers = {}
        monkeypatch.setattr(subagent_worker, "MCPClient", lambda: mcp)

        async def _fake_build_context(*_a: object, **_k: object) -> TurnContext:
            return TurnContext(
                thread_id="thread-1",
                request_id="req-1",
                agent_md="# Agent",
                memory_index=[],
                skills=[],
                tools=[],
                user_id=None,
                parent_run_id=envelope.parent_run_id,
                model="gemini-2.5-flash",
            )

        monkeypatch.setattr(subagent_worker, "build_context", _fake_build_context)

        async def _fake_run_loop(*_a: object, **_k: object):
            yield TurnComplete(
                request_id="req-1",
                usage=UsageTotals(
                    input_tokens=1,
                    output_tokens=1,
                    cached_tokens=0,
                    cost_usd=0.0,
                    duration_ms=1,
                    estimated_prompt_tokens=0,
                ),
            )

        monkeypatch.setattr(subagent_worker, "run_loop", _fake_run_loop)

        def _init_with_memory_exporter() -> bool:
            from monkeybot.observability import init_observability

            provider = trace.get_tracer_provider()
            if hasattr(provider, "add_span_processor"):
                provider.add_span_processor(SimpleSpanProcessor(otel_memory_exporter))
            return init_observability()

        monkeypatch.setattr(subagent_worker, "init_observability", _init_with_memory_exporter)
        monkeypatch.setattr(subagent_worker, "shutdown_observability", lambda: None)

        await subagent_worker._async_main()
        yield TurnComplete(
            request_id="req-1",
            usage=UsageTotals(
                input_tokens=1,
                output_tokens=1,
                cached_tokens=0,
                cost_usd=0.0,
                duration_ms=1,
                estimated_prompt_tokens=0,
            ),
        )

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)
    worker_script = tmp_path / "subagent_worker.py"
    worker_script.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker_script))
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# Test agent\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_MD", str(agent_md))

    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(mem_root),
        skills_path=tmp_path / "skills2",
        mcp=_NoMCP(),
    )
    (tmp_path / "skills2").mkdir(exist_ok=True)

    prov = FakeProvider(
        [
            [ToolCall(call_id="c1", name="task", args={"task": "do work", "context": ""})],
            [TextDelta(text="done"), Done()],
        ]
    )
    ctx = _ctx_with_task()
    ctx = TurnContext(
        thread_id=ctx.thread_id,
        request_id=ctx.request_id,
        agent_md=ctx.agent_md,
        memory_index=ctx.memory_index,
        skills=ctx.skills,
        tools=ctx.tools,
        user_id=ctx.user_id,
        parent_run_id="parent-run-1",
        model=ctx.model,
    )

    async for _ in run(
        "delegate",
        ctx,
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=ex,
        max_turns=4,
    ):
        pass

    spans = otel_memory_exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert "monkeybot.run" in names
    assert "monkeybot.tool" in names
    assert "monkeybot.subagent" in names

    parent_run = next(
        s
        for s in spans
        if s.name == "monkeybot.run" and s.attributes.get("trace.input") == "delegate"
    )
    parent_trace_id = parent_run.context.trace_id
    sub = next(s for s in spans if s.name == "monkeybot.subagent")
    assert sub.context.trace_id == parent_trace_id
    tool = next(s for s in spans if s.name == "monkeybot.tool")
    assert tool.context.trace_id == parent_trace_id
