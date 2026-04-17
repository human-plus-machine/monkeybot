"""HTTP tests for ``/harness/identity/*`` (Story 5).

Mount the identity router on a throwaway FastAPI app, attach a fresh
:class:`IdentityResolutionMW` to ``app.state.identity_mw``, and exercise
the bust + stats endpoints + admin-auth guard via ``fastapi.TestClient``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.core.harness.events import Principal  # noqa: E402
from src.core.harness.extensions import CallableIdentitySource  # noqa: E402
from src.core.harness.extensions.values import LoadedIdentity  # noqa: E402
from src.core.harness.middleware.identity_resolution import (  # noqa: E402
    IdentityResolutionMW,
)
from src.gateway.harness_identity_routes import router  # noqa: E402


def _identity(principal_id: str) -> LoadedIdentity:
    return LoadedIdentity(
        principal_id=principal_id,
        soul="s",
        rules="r",
        identity="i",
        user="u",
        index="ix",
        memory="m",
        heartbeat="h",
        loaded_at=datetime.now(UTC),
        ttl_seconds=60,
        source_backend="callable",
    )


def _build_app() -> tuple[FastAPI, IdentityResolutionMW]:
    async def fn(principal: Principal, _session_id: str | None) -> LoadedIdentity:
        return _identity(principal.id)

    mw = IdentityResolutionMW(CallableIdentitySource(fn))
    app = FastAPI()
    app.state.identity_mw = mw
    app.include_router(router)
    return app, mw


def test_cache_stats_returns_counters() -> None:
    """``GET /harness/identity/cache/stats`` returns a stats snapshot."""
    app, mw = _build_app()
    mw.cache.put(("alice", "s1"), _identity("alice"), ttl_seconds=60)
    with TestClient(app) as client:
        resp = client.get("/harness/identity/cache/stats")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["size"] == 1
    assert data["capacity"] == mw.cache.capacity


def test_bust_requires_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requests without a valid admin token get ``403``."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")
    app, _ = _build_app()
    with TestClient(app) as client:
        resp = client.post("/harness/identity/bust", json={"principal_id": "alice"})
    assert resp.status_code == 403


def test_bust_invalidates_matching_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid admin bust evicts exactly the keys that match the body filter."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")
    app, mw = _build_app()
    mw.cache.put(("alice", "s1"), _identity("alice"), ttl_seconds=60)
    mw.cache.put(("alice", "s2"), _identity("alice"), ttl_seconds=60)
    mw.cache.put(("bob", "s1"), _identity("bob"), ttl_seconds=60)

    with TestClient(app) as client:
        resp = client.post(
            "/harness/identity/bust",
            json={"principal_id": "alice"},
            headers={"X-Admin-Token": "s3cret"},
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["entries_invalidated"] == 2
    assert mw.cache.stats()["size"] == 1


def test_bust_all_with_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty body wipes the cache (bust-all still requires admin auth)."""
    monkeypatch.setenv("EMONK_ADMIN_TOKEN", "s3cret")
    app, mw = _build_app()
    mw.cache.put(("alice", "s"), _identity("alice"), ttl_seconds=60)
    mw.cache.put(("bob", "s"), _identity("bob"), ttl_seconds=60)

    with TestClient(app) as client:
        resp = client.post(
            "/harness/identity/bust",
            json={},
            headers={"X-Admin-Token": "s3cret"},
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["entries_invalidated"] == 2
    assert mw.cache.stats()["size"] == 0


def test_returns_503_when_mw_not_configured() -> None:
    """Without ``app.state.identity_mw`` the endpoint fails with 503."""
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        resp = client.get("/harness/identity/cache/stats")
    assert resp.status_code == 503
