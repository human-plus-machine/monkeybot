"""FastAPI router for ``/harness/secrets/*`` admin endpoints (Story 6).

Endpoints
---------
``GET /harness/secrets/health`` — admin-only per-leg reachability probe.
    Each leg of the configured :class:`SecretResolver` (or the single
    resolver if not composite) is probed with a known-missing handle;
    :class:`SecretNotFound` counts as "reachable" (the resolver can
    answer), while any other exception records ``reachable=False``. Handle
    values are **never** included in the response.

The router reads the live :class:`SecretResolver` from
``app.state.secret_resolver``. Consumers populate that slot from the built
:class:`CompiledAgent`::

    app.state.secret_resolver = compiled.secret_resolver

If the slot is empty the endpoint returns ``503``.

Admin auth: reuses :func:`src.gateway.harness_identity_routes.admin_auth`
so a single ``EMONK_ADMIN_TOKEN`` governs every admin surface.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from src.core.harness.extensions import SecretNotFound
from src.core.harness.extensions.secret_resolvers.composite import (
    CompositeSecretResolver,
)

from .harness_identity_routes import admin_auth

router = APIRouter(prefix="/harness/secrets", tags=["harness:secrets"])

_PROBE_HANDLE = "__health_probe__"


def _resolver_from_app(request: Request) -> Any:
    """Pull the live :class:`SecretResolver` off ``app.state`` or 503."""
    resolver = getattr(request.app.state, "secret_resolver", None)
    if resolver is None:
        raise HTTPException(
            status_code=503,
            detail="secret resolver not configured: app.state.secret_resolver missing",
        )
    return resolver


def _legs_of(resolver: Any) -> list[Any]:
    """Return the list of legs to probe.

    For composite resolvers we probe each leg individually so the caller
    can see which backend is unhealthy. For a tracing wrapper we unwrap
    once so the underlying resolver type is reported. For every other
    resolver we probe the resolver itself.
    """
    inner = resolver
    if hasattr(resolver, "inner") and not isinstance(resolver, CompositeSecretResolver):
        inner = resolver.inner
    if isinstance(inner, CompositeSecretResolver):
        return list(inner.chain)
    return [inner]


async def _probe(leg: Any) -> dict[str, Any]:
    """Probe a single resolver and return its reachability dict."""
    start = time.monotonic()
    try:
        await leg.resolve(_PROBE_HANDLE)
        status: dict[str, Any] = {
            "resolver": type(leg).__name__,
            "reachable": True,
            "notes": "unexpected_success",
        }
    except SecretNotFound:
        status = {
            "resolver": type(leg).__name__,
            "reachable": True,
            "notes": "probe_not_found_ok",
        }
    except Exception as exc:  # noqa: BLE001 - intentionally broad probe
        status = {
            "resolver": type(leg).__name__,
            "reachable": False,
            "error_class": type(exc).__name__,
        }
    status["latency_ms"] = int((time.monotonic() - start) * 1000)
    return status


@router.get("/health", dependencies=[Depends(admin_auth)])
async def health(request: Request) -> dict[str, Any]:
    """Return per-leg reachability for the configured resolver chain."""
    resolver = _resolver_from_app(request)
    legs = _legs_of(resolver)
    out: list[dict[str, Any]] = []
    for leg in legs:
        out.append(await _probe(leg))
    return {"data": {"legs": out}}


__all__ = ["router"]
