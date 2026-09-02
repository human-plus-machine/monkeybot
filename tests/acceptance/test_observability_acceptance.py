"""Phase 7 agent-automatable acceptance checks (observability AC-*)."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_TRACE_ID_HEX = re.compile(r"^[0-9a-f]{32}$")


def _tier_policy_yaml() -> str:
    return "deny_patterns: []\n"


def _parse_sse_data_lines(body: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                raw = line[len("data:") :].strip()
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    return events


def _reset_otel_globals() -> None:
    from opentelemetry import trace

    from monkeybot.observability import _state

    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    _state._initialized = False
    _state._enabled = False


@pytest_asyncio.fixture
async def gateway_client_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[AsyncClient]:
    from asgi_lifespan import LifespanManager

    from monkeybot.core.mcp.mcp_client import MCPClient
    from monkeybot.gateway.sse.app import app
    from monkeybot.observability import shutdown_observability

    shutdown_observability()
    _reset_otel_globals()
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "false")

    db_file = tmp_path / "mb.db"
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "model:\n  provider: fake\n  name: fake\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "no_mcp.json"))
    policy_file = tmp_path / "command_allowlist.yaml"
    policy_file.write_text(_tier_policy_yaml(), encoding="utf-8")
    monkeypatch.setenv("COMMAND_ALLOWLIST_CONFIG", str(policy_file))
    agent = tmp_path / "AGENT.md"
    agent.write_text("# Test agent\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_MD", str(agent))
    memory = tmp_path / "memory"
    memory.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("MEMORY_PATH", str(memory))
    monkeypatch.setenv("SKILLS_PATH", str(skills))

    async def _skip_mcp_load(self: MCPClient, _path: object, *_a: object, **_kw: object) -> None:
        return

    monkeypatch.setattr(MCPClient, "load_from_config", _skip_mcp_load)

    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest_asyncio.fixture
async def gateway_client_otel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, InMemorySpanExporter]]:
    from asgi_lifespan import LifespanManager

    from monkeybot.core.mcp.mcp_client import MCPClient
    from monkeybot.gateway.sse.app import app
    from monkeybot.observability import shutdown_observability

    exporter = InMemorySpanExporter()

    def _memory_processor(_kind: str) -> SimpleSpanProcessor:
        return SimpleSpanProcessor(exporter)

    shutdown_observability()
    _reset_otel_globals()
    monkeypatch.setattr("monkeybot.observability._create_span_processor", _memory_processor)
    monkeypatch.setenv("MONKEYBOT_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "console")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "none")

    db_file = tmp_path / "mb.db"
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "model:\n  provider: fake\n  name: fake\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "no_mcp.json"))
    policy_file = tmp_path / "command_allowlist.yaml"
    policy_file.write_text(_tier_policy_yaml(), encoding="utf-8")
    monkeypatch.setenv("COMMAND_ALLOWLIST_CONFIG", str(policy_file))
    agent = tmp_path / "AGENT.md"
    agent.write_text("# Test agent\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_MD", str(agent))
    memory = tmp_path / "memory"
    memory.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("MEMORY_PATH", str(memory))
    monkeypatch.setenv("SKILLS_PATH", str(skills))

    async def _skip_mcp_load(self: MCPClient, _path: object, *_a: object, **_kw: object) -> None:
        return

    monkeypatch.setattr(MCPClient, "load_from_config", _skip_mcp_load)

    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, exporter

    shutdown_observability()
    _reset_otel_globals()


@pytest.mark.acceptance
@pytest.mark.asyncio
async def test_ac_001_tracing_disabled_gateway_reply(gateway_client_disabled: AsyncClient) -> None:
    """AC-001: agent works with MONKEYBOT_OTEL_ENABLED=false."""
    from monkeybot.gateway.sse.app import app
    from monkeybot.observability import is_observability_enabled

    assert is_observability_enabled() is False
    r1 = await gateway_client_disabled.post("/sessions", json={})
    assert r1.status_code == 201
    sid = r1.json()["session_id"]
    bus = app.state.registry.get(sid)
    assert bus is not None
    _replay, q = await bus.subscribe(None)
    r2 = await gateway_client_disabled.post(
        f"/sessions/{sid}/reply",
        json={"request_id": "ac-001", "message": "Hello"},
    )
    assert r2.status_code == 200
    joined = ""
    deadline = asyncio.get_running_loop().time() + 10.0
    while '"type":"TurnComplete"' not in joined and asyncio.get_running_loop().time() < deadline:
        joined += await asyncio.wait_for(q.get(), timeout=1.0)
    parsed = _parse_sse_data_lines(joined)
    assert any(p.get("type") == "TurnComplete" for p in parsed)


@pytest.mark.acceptance
@pytest.mark.asyncio
async def test_ac_002_nested_spans_on_gateway_turn(
    gateway_client_otel: tuple[AsyncClient, InMemorySpanExporter],
) -> None:
    """AC-002: run → turn → llm spans on one gateway message (in-memory export)."""
    client, exporter = gateway_client_otel
    from monkeybot.gateway.sse.app import app

    r1 = await client.post("/sessions", json={})
    sid = r1.json()["session_id"]
    bus = app.state.registry.get(sid)
    assert bus is not None
    _replay, q = await bus.subscribe(None)
    await client.post(f"/sessions/{sid}/reply", json={"request_id": "ac-002", "message": "Hi"})
    joined = ""
    deadline = asyncio.get_running_loop().time() + 10.0
    while '"type":"TurnComplete"' not in joined and asyncio.get_running_loop().time() < deadline:
        joined += await asyncio.wait_for(q.get(), timeout=1.0)

    names = {s.name for s in exporter.get_finished_spans()}
    assert "monkeybot.run" in names
    assert "monkeybot.turn" in names
    assert "monkeybot.llm.stream" in names
    run_span = next(s for s in exporter.get_finished_spans() if s.name == "monkeybot.run")
    llm = next(s for s in exporter.get_finished_spans() if s.name == "monkeybot.llm.stream")
    assert run_span.attributes.get("thread.id")
    assert run_span.attributes.get("request.id")
    assert llm.attributes.get("gen_ai.request.model")


@pytest.mark.acceptance
@pytest.mark.asyncio
async def test_ac_014_turn_complete_trace_id_in_sse(
    gateway_client_otel: tuple[AsyncClient, InMemorySpanExporter],
) -> None:
    """AC-014: TurnComplete includes trace_id when tracing enabled."""
    client, _exporter = gateway_client_otel
    from monkeybot.gateway.sse.app import app

    r1 = await client.post("/sessions", json={})
    sid = r1.json()["session_id"]
    bus = app.state.registry.get(sid)
    assert bus is not None
    _replay, q = await bus.subscribe(None)
    await client.post(f"/sessions/{sid}/reply", json={"request_id": "ac-014", "message": "Hi"})
    joined = ""
    deadline = asyncio.get_running_loop().time() + 10.0
    while '"type":"TurnComplete"' not in joined and asyncio.get_running_loop().time() < deadline:
        joined += await asyncio.wait_for(q.get(), timeout=1.0)
    parsed = _parse_sse_data_lines(joined)
    tc = next(p for p in parsed if p.get("type") == "TurnComplete")
    trace_id = tc.get("trace_id")
    assert isinstance(trace_id, str)
    assert _TRACE_ID_HEX.match(trace_id)


@pytest.mark.acceptance
def test_ac_013_runbook_documents_production_sampling() -> None:
    """AC-013: production sampling env documented in runbook."""
    text = (Path(__file__).resolve().parents[2] / "docs" / "observability-runbook.md").read_text(
        encoding="utf-8"
    )
    assert "parentbased_traceidratio" in text
    assert "OTEL_TRACES_SAMPLER_ARG=0.1" in text or "OTEL_TRACES_SAMPLER_ARG` | `0.1`" in text
