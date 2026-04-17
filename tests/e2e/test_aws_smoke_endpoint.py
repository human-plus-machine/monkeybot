"""E2E tests for ``GET /harness/aws/smoke`` (Story 8).

Mounts the AWS smoke router on a throwaway FastAPI app and exercises three
response paths:

* ``403`` when the ``X-Admin-Token`` header is missing/mismatched.
* ``503`` when the ``HARNESS_ENABLE_AWS_SMOKE`` feature flag is unset.
* ``200`` with all six probes reachable when admin auth + feature flag are
  present and the six probe callables are monkeypatched to async no-ops.

The tests also monkeypatch :mod:`boto3` / :mod:`aioboto3` / :mod:`asyncpg`
wholesale via ``sys.modules`` injection so the import lines inside the probe
bodies never hit the real SDKs — the test suite must run entirely offline.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.gateway import harness_aws_routes  # noqa: E402
from src.gateway.harness_aws_routes import router as aws_router  # noqa: E402


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(aws_router)
    return app


def _install_async_noop(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    async def _fn() -> None:
        return None

    monkeypatch.setattr(harness_aws_routes, name, _fn)


def _patch_all_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    for attr in (
        "_probe_postgres",
        "_probe_s3",
        "_probe_secrets_manager",
        "_probe_bedrock",
        "_probe_kms",
        "_probe_sts",
    ):
        _install_async_noop(monkeypatch, attr)


def test_smoke_requires_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``X-Admin-Token`` the endpoint returns ``403``."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")
    monkeypatch.setenv("HARNESS_ENABLE_AWS_SMOKE", "1")
    _patch_all_probes(monkeypatch)

    app = _build_app()
    with TestClient(app) as client:
        resp = client.get("/harness/aws/smoke")

    assert resp.status_code == 403


def test_smoke_rejects_wrong_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mismatched admin token receives ``403`` (same fail-closed posture)."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")
    monkeypatch.setenv("HARNESS_ENABLE_AWS_SMOKE", "1")
    _patch_all_probes(monkeypatch)

    app = _build_app()
    with TestClient(app) as client:
        resp = client.get(
            "/harness/aws/smoke",
            headers={"X-Admin-Token": "wrong"},
        )

    assert resp.status_code == 403


def test_smoke_503_when_feature_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``HARNESS_ENABLE_AWS_SMOKE=1`` the endpoint returns ``503``."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")
    monkeypatch.delenv("HARNESS_ENABLE_AWS_SMOKE", raising=False)
    _patch_all_probes(monkeypatch)

    app = _build_app()
    with TestClient(app) as client:
        resp = client.get(
            "/harness/aws/smoke",
            headers={"X-Admin-Token": "s3cret"},
        )

    assert resp.status_code == 503
    assert "HARNESS_ENABLE_AWS_SMOKE" in resp.json()["detail"]


def test_smoke_503_when_feature_flag_not_exactly_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Values other than ``"1"`` also fail closed."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")
    monkeypatch.setenv("HARNESS_ENABLE_AWS_SMOKE", "true")
    _patch_all_probes(monkeypatch)

    app = _build_app()
    with TestClient(app) as client:
        resp = client.get(
            "/harness/aws/smoke",
            headers={"X-Admin-Token": "s3cret"},
        )

    assert resp.status_code == 503


def test_smoke_returns_all_six_probes_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All six probes succeed when the probe callables are async no-ops."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")
    monkeypatch.setenv("HARNESS_ENABLE_AWS_SMOKE", "1")
    _patch_all_probes(monkeypatch)

    app = _build_app()
    with TestClient(app) as client:
        resp = client.get(
            "/harness/aws/smoke",
            headers={"X-Admin-Token": "s3cret"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["all_pass"] is True
    assert body["data"]["ok"] is True
    checks = body["data"]["checks"]
    assert len(checks) == 6
    names = sorted(c["name"] for c in checks)
    assert names == [
        "aws_secrets_manager.list",
        "bedrock.list_foundation_models",
        "kms.describe_key",
        "postgres.ckpt.ping",
        "s3.memory.head_bucket",
        "sts.get_caller_identity",
    ]
    for check in checks:
        assert check["status"] == "pass"
        assert check["reachable"] is True
        assert isinstance(check["latency_ms"], int)
        assert check["latency_ms"] >= 0
        assert "error_class" not in check


def test_smoke_reports_unreachable_probe_without_leaking_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe raising surfaces ``reachable=False`` with only the class name."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")
    monkeypatch.setenv("HARNESS_ENABLE_AWS_SMOKE", "1")

    for attr in (
        "_probe_s3",
        "_probe_secrets_manager",
        "_probe_bedrock",
        "_probe_kms",
        "_probe_sts",
    ):
        _install_async_noop(monkeypatch, attr)

    class _BoomError(Exception):
        pass

    async def _failing() -> None:
        raise _BoomError("super-secret-dsn=postgres://user:pass@host/db")

    monkeypatch.setattr(harness_aws_routes, "_probe_postgres", _failing)

    app = _build_app()
    with TestClient(app) as client:
        resp = client.get(
            "/harness/aws/smoke",
            headers={"X-Admin-Token": "s3cret"},
        )

    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "HARNESS_AWS_SMOKE_FAIL"
    assert body["data"]["all_pass"] is False
    assert body["data"]["ok"] is False
    postgres = next(
        c for c in body["data"]["checks"] if c["name"] == "postgres.ckpt.ping"
    )
    assert postgres["status"] == "fail"
    assert postgres["reachable"] is False
    assert postgres["error_class"] == "_BoomError"
    assert "super-secret-dsn" not in resp.text
    assert "postgres://" not in resp.text


def test_probe_postgres_requires_ckpt_dsn_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_probe_postgres`` fails fast with ``RuntimeError`` when ``CKPT_DSN`` is unset."""
    monkeypatch.delenv("CKPT_DSN", raising=False)

    async def _run() -> None:
        await harness_aws_routes._probe_postgres()

    asyncpg_mod = types.ModuleType("asyncpg")
    asyncpg_mod.connect = lambda *a, **k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "asyncpg", asyncpg_mod)

    import asyncio

    with pytest.raises(RuntimeError, match="CKPT_DSN not set"):
        asyncio.run(_run())


def test_probe_s3_uses_head_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_probe_s3`` calls ``head_bucket`` against the configured bucket."""
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    monkeypatch.delenv("AWS_REGION", raising=False)

    calls: list[dict[str, Any]] = []

    class _FakeS3:
        async def head_bucket(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    class _FakeCtx:
        async def __aenter__(self) -> _FakeS3:
            return _FakeS3()

        async def __aexit__(self, *exc: Any) -> None:
            return None

    def _fake_s3_client(region: str | None = None) -> _FakeCtx:
        calls.append({"_region": region})
        return _FakeCtx()

    import src.core.harness.extensions._aws_clients as aws_clients

    monkeypatch.setattr(aws_clients, "s3_client", _fake_s3_client)

    import asyncio

    asyncio.run(harness_aws_routes._probe_s3())
    assert any(c.get("Bucket") == "my-bucket" for c in calls)
