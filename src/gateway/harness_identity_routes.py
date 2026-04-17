"""FastAPI router for ``/harness/identity/*`` admin endpoints (Story 5).

Endpoints
---------
``POST /harness/identity/bust`` — admin-only cache bust. Accepts an optional
    ``principal_id`` and/or ``session_id``; returns the number of cache
    entries invalidated. Bust-all requires the admin token.
``GET  /harness/identity/cache/stats`` — returns the cache stats snapshot
    (size, capacity, hit/miss counters).

The router reads the live :class:`IdentityResolutionMW` from
``app.state.identity_mw``. Consumers are expected to populate that slot
from the built :class:`CompiledAgent`::

    app.state.identity_mw = compiled.middleware_of(IdentityResolutionMW)

If the slot is empty the endpoints return 503 so deployments without the
middleware configured fail loudly instead of silently no-oping.

Admin auth: an ``X-Admin-Token`` header whose value matches the
``EMONK_ADMIN_TOKEN`` env var. TODO(Phase 6): swap this for the canonical
admin-auth dependency once the gateway-wide helper lands.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/harness/identity", tags=["harness:identity"])


class BustRequest(BaseModel):
    """Body for ``POST /harness/identity/bust``."""

    principal_id: str | None = None
    session_id: str | None = None
    reason: str | None = None


async def admin_auth(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    """Reject the request unless ``X-Admin-Token`` matches ``EMONK_ADMIN_TOKEN``.

    Raises:
        HTTPException(403): Token is missing / mismatched, or the env var is
            unset (fail-closed so a forgotten deployment variable cannot
            silently disable auth).
    """
    expected = os.environ.get("EMONK_ADMIN_TOKEN")
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=403, detail="admin auth required")


def _get_mw(request: Request) -> Any:
    mw = getattr(request.app.state, "identity_mw", None)
    if mw is None:
        raise HTTPException(
            status_code=503,
            detail="identity middleware not configured: app.state.identity_mw missing",
        )
    return mw


def _matches(key: tuple[str, str], principal_id: str | None, session_id: str | None) -> bool:
    pid, sid = key
    if principal_id is None and session_id is None:
        return True
    if principal_id is not None and principal_id != pid:
        return False
    if session_id is not None:
        actual = sid if sid != "__no_session__" else None
        if actual != session_id:
            return False
    return True


@router.post("/bust", dependencies=[Depends(admin_auth)])
async def bust(body: BustRequest, request: Request) -> dict[str, Any]:
    """Invalidate cache entries matching ``principal_id`` / ``session_id``.

    Returns the number of evictions under ``data.entries_invalidated``.
    ``invalidate`` fires the middleware's ``on_evict`` callback which
    publishes an :attr:`EventKind.IDENTITY_BUST` event per evicted entry
    when an :class:`EventBus` is wired on the middleware.
    """
    mw = _get_mw(request)

    def predicate(key: Any) -> bool:
        if not isinstance(key, tuple) or len(key) != 2:
            return False
        return _matches((str(key[0]), str(key[1])), body.principal_id, body.session_id)

    count = mw.cache.invalidate(predicate)
    return {
        "data": {
            "entries_invalidated": count,
            "principal_id": body.principal_id,
            "session_id": body.session_id,
            "reason": body.reason,
        }
    }


@router.get("/cache/stats")
async def cache_stats(request: Request) -> dict[str, Any]:
    """Return the live cache counters (no auth — stats are non-sensitive)."""
    mw = _get_mw(request)
    return {"data": mw.cache.stats()}


__all__ = ["admin_auth", "router"]
