"""E2E tests for ``GET /harness/secrets/health`` (Story 6).

Mount the secrets router on a throwaway FastAPI app, inject a composite
:class:`SecretResolver` into ``app.state.secret_resolver``, and exercise
the admin-auth guard plus the per-leg reachability report via
``fastapi.TestClient``.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.core.harness.extensions._mocks import MockSecretResolver  # noqa: E402
from src.core.harness.extensions.secret_resolvers import (  # noqa: E402
    CompositeSecretResolver,
    EnvSecretResolver,
)
from src.gateway.harness_secrets_routes import router  # noqa: E402


def _build_app(resolver: object | None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    if resolver is not None:
        app.state.secret_resolver = resolver
    return app


def test_health_requires_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requests without ``X-Admin-Token`` receive ``403``."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")
    resolver = CompositeSecretResolver(chain=[EnvSecretResolver(), MockSecretResolver({})])
    app = _build_app(resolver)
    with TestClient(app) as client:
        resp = client.get("/harness/secrets/health")
    assert resp.status_code == 403


def test_health_returns_legs_with_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid admin token returns ``200`` with per-leg reachability entries."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")
    resolver = CompositeSecretResolver(
        chain=[EnvSecretResolver(prefix="PROBE_"), MockSecretResolver({})]
    )
    app = _build_app(resolver)

    with TestClient(app) as client:
        resp = client.get(
            "/harness/secrets/health",
            headers={"X-Admin-Token": "s3cret"},
        )

    assert resp.status_code == 200
    legs = resp.json()["data"]["legs"]
    assert len(legs) == 2
    names = [leg["resolver"] for leg in legs]
    assert names == ["EnvSecretResolver", "MockSecretResolver"]
    for leg in legs:
        assert leg["reachable"] is True
        assert leg["notes"] == "probe_not_found_ok"
        assert isinstance(leg["latency_ms"], int)
        assert "__health_probe__" not in str(leg)


def test_health_reports_unreachable_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leg raising an unexpected error is reported ``reachable=False``."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")

    class _BoomResolver(MockSecretResolver):
        async def resolve(self, handle: str):  # type: ignore[override]
            raise RuntimeError("network down")

    resolver = CompositeSecretResolver(chain=[_BoomResolver(), MockSecretResolver({})])
    app = _build_app(resolver)

    with TestClient(app) as client:
        resp = client.get(
            "/harness/secrets/health",
            headers={"X-Admin-Token": "s3cret"},
        )

    assert resp.status_code == 200
    legs = resp.json()["data"]["legs"]
    assert legs[0]["reachable"] is False
    assert legs[0]["error_class"] == "RuntimeError"
    assert legs[1]["reachable"] is True


def test_health_handles_single_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-composite resolver is probed as a single leg."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")
    resolver = EnvSecretResolver(prefix="PROBE_")
    app = _build_app(resolver)

    with TestClient(app) as client:
        resp = client.get(
            "/harness/secrets/health",
            headers={"X-Admin-Token": "s3cret"},
        )

    assert resp.status_code == 200
    legs = resp.json()["data"]["legs"]
    assert len(legs) == 1
    assert legs[0]["resolver"] == "EnvSecretResolver"
    assert legs[0]["reachable"] is True


def test_health_returns_503_when_resolver_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``app.state.secret_resolver`` the endpoint fails closed with ``503``."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")
    app = _build_app(None)
    with TestClient(app) as client:
        resp = client.get(
            "/harness/secrets/health",
            headers={"X-Admin-Token": "s3cret"},
        )
    assert resp.status_code == 503


def test_probe_handle_never_appears_in_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """The response body never contains the probe handle or any handle value."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")
    resolver = CompositeSecretResolver(
        chain=[MockSecretResolver({"__health_probe__": "leaked"})]
    )
    app = _build_app(resolver)

    with TestClient(app) as client:
        resp = client.get(
            "/harness/secrets/health",
            headers={"X-Admin-Token": "s3cret"},
        )

    body = resp.text
    assert resp.status_code == 200
    # A successful probe is unexpected but still must not leak the value.
    assert "leaked" not in body
    # We still allow the probe handle literal elsewhere in the codebase but
    # never in the response payload.
    assert "__health_probe__" not in body


def test_unexpected_success_recorded_as_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolver that accidentally returns a value for the probe handle stays reachable."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")
    # MockSecretResolver raises SecretNotFound for missing handles; bind the
    # probe handle explicitly to simulate the unexpected-success path.
    resolver = MockSecretResolver({"__health_probe__": "oops"})
    # sanity: ensure the resolver really returns a value for the probe handle
    assert resolver._secrets["__health_probe__"] == "oops"
    app = _build_app(resolver)

    with TestClient(app) as client:
        resp = client.get(
            "/harness/secrets/health",
            headers={"X-Admin-Token": "s3cret"},
        )

    assert resp.status_code == 200
    legs = resp.json()["data"]["legs"]
    assert legs[0]["reachable"] is True
    assert legs[0]["notes"] == "unexpected_success"
    # SecretNotFound was never raised; nonetheless no value is leaked.
    assert "oops" not in resp.text
