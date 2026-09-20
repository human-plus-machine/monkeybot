"""Shared FastAPI dependency for routes backed by durable storage."""

from __future__ import annotations

import uuid

from fastapi import Request

from monkeybot.core.persistence.backends import StorageBackend
from monkeybot.gateway.sse.models import APIError


def require_storage_backend(request: Request) -> StorageBackend:
    """Return initialized app storage or a consistent service-unavailable error."""
    backend: StorageBackend | None = getattr(request.app.state, "storage", None)
    if backend is None:
        raise APIError(
            503,
            "STORAGE_NOT_READY",
            "Storage backend not initialized",
            uuid.uuid4().hex,
        )
    return backend
