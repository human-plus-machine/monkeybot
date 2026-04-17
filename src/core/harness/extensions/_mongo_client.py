"""Shared async Mongo client helper.

One ``AsyncIOMotorClient`` is memoised per ``uri_env`` so backends targeting
the same cluster share a connection pool. Emits a health-degraded signal the
first time a non-replica-set deployment is detected, so the scheduler can
opt out of multi-document transactions.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Mapping
from typing import Any

from .errors import BackendConfigError

logger = logging.getLogger(__name__)

_CLIENTS: dict[str, Any] = {}
_WARN_NO_RS: set[str] = set()


def _emit_health_degraded(code: str, context: Mapping[str, Any]) -> None:
    """Log a health-degraded signal. Replaced by event bus wiring in later stories."""
    logger.warning("harness.health.degraded code=%s context=%s", code, dict(context))


async def get_client(*, uri_env: str) -> Any:
    """Return the cached Motor client for ``uri_env`` (creating on first call).

    Raises:
        BackendConfigError: ``uri_env`` is unset or the motor import fails.
    """
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError as exc:  # pragma: no cover - optional dep
        raise BackendConfigError(
            "Mongo backend requires emonk[checkpointer-mongo] (motor)"
        ) from exc

    client = _CLIENTS.get(uri_env)
    if client is None:
        uri = os.environ.get(uri_env)
        if not uri:
            raise BackendConfigError(
                f"Mongo backend: environment variable {uri_env!r} is not set"
            )
        client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=2000)
        _CLIENTS[uri_env] = client

    try:
        hello = await client.admin.command("hello")
        if not hello.get("setName") and uri_env not in _WARN_NO_RS:
            _WARN_NO_RS.add(uri_env)
            _emit_health_degraded("mongo_no_replica_set", {"uri_env": uri_env})
    except Exception:  # noqa: BLE001 - smoke-only; real errors surface on ops
        pass
    return client


async def close_all() -> None:
    """Close every cached Motor client. Intended for test teardown."""
    clients = list(_CLIENTS.values())
    _CLIENTS.clear()
    _WARN_NO_RS.clear()
    for client in clients:
        with contextlib.suppress(Exception):  # pragma: no cover - defensive teardown
            client.close()


__all__ = ["close_all", "get_client"]
