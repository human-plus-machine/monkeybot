"""GCP Secret Manager-backed :class:`SecretResolver` (Story 6).

The ``google.cloud.secretmanager`` client is imported lazily so this module
is safe to import without the optional dependency installed. Blocking
gRPC calls are wrapped in :func:`asyncio.to_thread` so the resolver stays
usable from async contexts.

Caching reuses :class:`IdentityCache` (Story 5).
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import SecretStr

from ...middleware._identity_cache import IdentityCache
from ..base import SecretResolver
from ..errors import BackendConfigError, SecretNotFound, SecretResolverError


class GCPSecretManagerResolver(SecretResolver):
    """Resolve secret handles via GCP Secret Manager.

    Each resolve reads ``projects/{project_id}/secrets/{handle}/versions/latest``.
    The returned payload is UTF-8 decoded and wrapped in :class:`SecretStr`.

    Args:
        project_id: GCP project id owning the secret namespace.
        cache_ttl_seconds: Soft TTL applied to each cached value.
        cache_capacity: Maximum number of live cached entries before LRU
            eviction.
        client: Optional pre-built client (used by tests to inject mocks).
    """

    def __init__(
        self,
        *,
        project_id: str,
        cache_ttl_seconds: int = 60,
        cache_capacity: int = 1024,
        client: Any | None = None,
    ) -> None:
        if not project_id:
            raise ValueError("GCPSecretManagerResolver requires a non-empty project_id")
        self.project_id = project_id
        self.cache_ttl_seconds = max(1, int(cache_ttl_seconds))
        self._cache = IdentityCache(capacity=max(1, int(cache_capacity)))
        self._client = client

    def _resource_name(self, handle: str) -> str:
        return f"projects/{self.project_id}/secrets/{handle}/versions/latest"

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from google.cloud import secretmanager  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - optional dep
            raise BackendConfigError(
                "GCPSecretManagerResolver requires emonk[secret-resolver-gcp] "
                "(google-cloud-secret-manager)"
            ) from exc
        self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    async def resolve(self, handle: str) -> SecretStr:
        """Return ``SecretStr`` for ``handle`` (cache-first).

        Raises:
            SecretNotFound: Secret Manager reports no matching secret.
            SecretResolverError: Transport / auth / permission failure.
        """
        cached = self._cache.get(handle)
        if cached is not None:
            return cached

        client = self._get_client()
        name = self._resource_name(handle)

        def _access() -> Any:
            return client.access_secret_version(request={"name": name})

        try:
            response = await asyncio.to_thread(_access)
        except Exception as exc:  # noqa: BLE001 - google grpc surface varies
            if _is_not_found(exc):
                raise SecretNotFound(handle) from exc
            raise SecretResolverError(handle, reason=type(exc).__name__) from exc

        payload = getattr(response, "payload", None)
        data = getattr(payload, "data", None) if payload is not None else None
        if data is None:
            raise SecretNotFound(handle)
        raw = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)

        value = SecretStr(raw)
        self._cache.put(handle, value, self.cache_ttl_seconds)
        return value


def _is_not_found(exc: Exception) -> bool:
    """Return ``True`` when ``exc`` indicates the secret does not exist."""
    name = type(exc).__name__
    if name == "NotFound":
        return True
    try:
        from google.api_core.exceptions import NotFound  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - optional dep
        not_found_cls: type[Exception] | None = None
    else:
        not_found_cls = NotFound
    if not_found_cls is not None and isinstance(exc, not_found_cls):
        return True
    return "not found" in str(exc).lower() and "permission" not in str(exc).lower()


__all__ = ["GCPSecretManagerResolver"]
