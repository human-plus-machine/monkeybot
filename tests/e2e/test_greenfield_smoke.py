"""End-to-end smoke test — spin the full harness + FastAPI + invoke an agent.

This test uses the same fallback stub agent path that a consumer sees when the
deep_agents wiring cannot resolve the model (e.g. no GCP credentials). It proves
the seven pillars integrate end to end: identity → rules → middleware → run
package → control plane.
"""

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
from src.core.harness.principal import make_user_principal


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seven_pillars_smoke(tmp_path: Path) -> None:
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "SOUL.md").write_text("I am a helpful assistant.")
    (mem / "IDENTITY.md").write_text("role: e2e-harness-smoke")
    (mem / "RULES.md").write_text("- [R-1] DENY_TOOL: git push\n- [R-2] DENY_SANDBOX_WRITE: /etc/**")
    (mem / "USER.md").write_text("user: pytest")

    cfg = HarnessConfig(
        agent=AgentSpec(name="e2e-smoke"),
        identity=IdentitySpec(dir=str(mem), enforce_rules=True),
        security=SecuritySpec(principal_required=True),
        sandbox=SandboxSpec(backend="local_shell"),
        observability=ObservabilitySpec(
            run_package=RunPackageSpec(writer="local", sink_uri=str(tmp_path / "runs"))
        ),
    )

    compiled = build_universal_agent(cfg)
    app = FastAPI()
    from src.gateway.harness_routes import router as hr
    from src.gateway.agentcore_routes import router as ar

    app.include_router(hr)
    app.include_router(ar)
    app.state.compiled_agent = compiled
    app.state.session_registry = compiled.session_registry
    app.state.run_package_writer = compiled.run_package_writer
    app.state.approval_channel = compiled.approval_channel

    result = await compiled.ainvoke(
        [{"role": "user", "content": "smoke"}],
        session_id="e2e-1",
        principal=make_user_principal(user_id="alice", email="alice@example.com"),
    )
    assert result["outcome"] == "pass"

    client = TestClient(app)
    intro = client.get("/harness/introspect").json()
    assert intro["mounted"] is True
    assert intro["sandbox_backend"] == "local_shell"

    runs = client.get("/harness/runs").json()
    assert any(r["run_id"] == result["run_id"] for r in runs)

    pkg = client.get(f"/harness/runs/{result['run_id']}").json()
    assert pkg["principal"]["id"] == "alice"
    assert pkg["outcome"] == "pass"

    report = client.get("/harness/introspect/e2e-1").json()
    assert report["status"] == "active"
    assert report["agent_name"] == "e2e-smoke"

    sessions = client.get("/harness/control/sessions").json()
    assert any(s["session_id"] == "e2e-1" for s in sessions)
