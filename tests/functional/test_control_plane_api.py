"""Functional tests for /harness/* FastAPI routes against a TestClient."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.harness import (
    AgentSpec,
    HarnessConfig,
    IdentitySpec,
    ObservabilitySpec,
    RunPackageSpec,
    SandboxSpec,
    SecuritySpec,
    build_universal_agent,
)
from src.gateway.harness_routes import router as harness_router
from src.gateway.agentcore_routes import router as agentcore_router


def _build_app(tmp_path: Path) -> tuple[FastAPI, object]:
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "RULES.md").write_text("- [R-1] DENY_TOOL: git push")
    cfg = HarnessConfig(
        agent=AgentSpec(name="control-plane-test"),
        identity=IdentitySpec(dir=str(mem), enforce_rules=True),
        security=SecuritySpec(principal_required=False),
        sandbox=SandboxSpec(backend="local_shell"),
        observability=ObservabilitySpec(
            run_package=RunPackageSpec(writer="local", sink_uri=str(tmp_path / "runs"))
        ),
    )
    compiled = build_universal_agent(cfg)
    app = FastAPI()
    app.include_router(harness_router)
    app.include_router(agentcore_router)
    app.state.compiled_agent = compiled
    app.state.session_registry = compiled.session_registry
    app.state.run_package_writer = compiled.run_package_writer
    app.state.approval_channel = compiled.approval_channel
    return app, compiled


def test_introspect_self(tmp_path: Path) -> None:
    app, compiled = _build_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/harness/introspect")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mounted"] is True
    assert data["agent_name"] == "control-plane-test"
    assert "PrincipalPropagationMW" in data["middleware"]


@pytest.mark.asyncio
async def test_pause_resume_cancel(tmp_path: Path) -> None:
    app, compiled = _build_app(tmp_path)
    await compiled.ainvoke(
        [{"role": "user", "content": "hi"}],
        session_id="sess-1",
        principal=__import__("src.core.harness.principal", fromlist=["make_user_principal"]).make_user_principal(user_id="u1"),
    )
    client = TestClient(app)
    assert client.post("/harness/control/sess-1/pause").json()["status"] == "paused"
    assert client.post("/harness/control/sess-1/resume").json()["status"] == "active"
    assert client.post("/harness/control/sess-1/cancel").json()["status"] == "cancelled"


def test_ping_agentcore(tmp_path: Path) -> None:
    app, _ = _build_app(tmp_path)
    client = TestClient(app)
    assert client.get("/agentcore/ping").json() == {"status": "ok"}


def test_agentcore_invocation(tmp_path: Path) -> None:
    app, _ = _build_app(tmp_path)
    client = TestClient(app)
    resp = client.post(
        "/agentcore/invocations",
        json={
            "inputText": "hello",
            "sessionId": "sess-ac-1",
            "sessionState": {"sessionAttributes": {"user_id": "alice"}},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sessionId"] == "sess-ac-1"
    assert "completion" in body
