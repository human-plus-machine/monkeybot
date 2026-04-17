"""Example-level smoke tests for the AWS enterprise reference stack.

These tests mount only the gateway routers (agentcore + harness + aws) onto a
throwaway FastAPI app and assert the shapes of the three endpoints operators
hit during deployment verification:

* ``GET /agentcore/ping``           — health probe
* ``GET /harness/aws/smoke``        — feature-flagged reachability probe
* ``GET /harness/introspect``       — harness version + mount status

No real AWS credentials are required — every probe is monkeypatched with
async stubs so the tests run entirely offline.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ``conftest.py`` (same directory) injects the monorepo root onto ``sys.path``
# so the monorepo's ``src/`` package shadows the example's empty placeholder.
from src.gateway import harness_aws_routes  # noqa: E402
from src.gateway.agentcore_routes import router as agentcore_router  # noqa: E402
from src.gateway.harness_aws_routes import router as aws_router  # noqa: E402
from src.gateway.harness_routes import router as harness_router  # noqa: E402


def _noop() -> Any:
    async def _fn() -> None:
        return None

    return _fn


def _build_app() -> FastAPI:
    app = FastAPI(title="aws-enterprise-agent-smoke")
    app.include_router(agentcore_router)
    app.include_router(harness_router)
    app.include_router(aws_router)
    return app


def test_agentcore_ping_returns_ok() -> None:
    """``/agentcore/ping`` returns the canonical AgentCore liveness payload."""
    app = _build_app()
    with TestClient(app) as client:
        resp = client.get("/agentcore/ping")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_harness_introspect_reports_unmounted_by_default() -> None:
    """Without ``app.state.compiled_agent``, introspect reports ``mounted=False``."""
    app = _build_app()
    with TestClient(app) as client:
        resp = client.get("/harness/introspect")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mounted"] is False
    assert body["harness_version"] == "1"


def test_aws_smoke_returns_ok_when_all_probes_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All six probes succeed → ``data.ok`` is true, every probe reports reachable."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "smoke-token")
    monkeypatch.setenv("HARNESS_ENABLE_AWS_SMOKE", "1")

    for probe_attr in (
        "_probe_postgres",
        "_probe_s3",
        "_probe_secrets_manager",
        "_probe_bedrock",
        "_probe_kms",
        "_probe_sts",
    ):
        monkeypatch.setattr(harness_aws_routes, probe_attr, _noop())

    app = _build_app()
    with TestClient(app) as client:
        resp = client.get(
            "/harness/aws/smoke",
            headers={"X-Admin-Token": "smoke-token"},
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["ok"] is True
    names = sorted(p["probe"] for p in data["probes"])
    assert names == ["bedrock", "kms", "postgres", "s3", "secrets_manager", "sts"]
    for probe in data["probes"]:
        assert probe["reachable"] is True
        assert isinstance(probe["latency_ms"], int)
