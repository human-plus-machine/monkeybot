"""AWS Secrets Manager-backed :class:`SecretResolver` (Story 6).

Uses the shared ``aioboto3`` session factory from
:mod:`src.core.harness.extensions._aws_clients` (owned by Story 3). The
``aioboto3`` import is lazy — just importing this module is safe even when
the optional dependency is not installed.

Caching is provided by :class:`IdentityCache` (Story 5), reused verbatim so
Story 6 does not duplicate LRU+TTL logic.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from ...middleware._identity_cache import IdentityCache
from .._aws_clients import secrets_client
from ..base import SecretResolver
from ..errors import SecretNotFound, SecretResolverError

_TRANSIENT_AWS_CODES = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "RequestLimitExceeded",
        "InternalServiceError",
        "ServiceUnavailableException",
        "TooManyRequestsException",
    }
)


class AWSSecretsManagerResolver(SecretResolver):
    """Resolve secret handles via AWS Secrets Manager ``GetSecretValue``.

    Args:
        region: Optional AWS region (``None`` defers to the SDK default
            resolution chain).
        cache_ttl_seconds: Soft TTL applied to each cached value.
        cache_capacity: Maximum number of live cached entries before LRU
            eviction kicks in.
    """

    def __init__(
        self,
        *,
        region: str | None = None,
        cache_ttl_seconds: int = 60,
        cache_capacity: int = 1024,
    ) -> None:
        self.region = region
        self.cache_ttl_seconds = max(1, int(cache_ttl_seconds))
        self._cache = IdentityCache(capacity=max(1, int(cache_capacity)))

    async def resolve(self, handle: str) -> SecretStr:
        """Return ``SecretStr`` for ``handle`` (cache-first).

        Raises:
            SecretNotFound: Secrets Manager reports no such secret.
            SecretResolverError: Transient / transport / auth failure.
        """
        cached = self._cache.get(handle)
        if cached is not None:
            return cached

        async with secrets_client(self.region) as client:
            try:
                response = await client.get_secret_value(SecretId=handle)
            except Exception as exc:  # noqa: BLE001 - botocore surface varies
                if _is_not_found(exc):
                    raise SecretNotFound(handle) from exc
                if _is_transient(exc):
                    raise SecretResolverError(handle, reason=type(exc).__name__) from exc
                raise SecretResolverError(handle, reason=type(exc).__name__) from exc

        raw = response.get("SecretString")
        if raw is None:
            binary = response.get("SecretBinary")
            if binary is None:
                raise SecretNotFound(handle)
            raw = binary.decode("utf-8") if isinstance(binary, (bytes, bytearray)) else str(binary)

        value = SecretStr(raw)
        self._cache.put(handle, value, self.cache_ttl_seconds)
        return value


def _error_code(exc: Any) -> str:
    """Best-effort extraction of the AWS error ``Code`` from a ``ClientError``."""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = resp.get("Error", {}).get("Code", "")
        if code:
            return str(code)
    return type(exc).__name__


def _is_not_found(exc: Exception) -> bool:
    code = _error_code(exc)
    if code in {"ResourceNotFoundException", "NoSuchKey", "NotFound"}:
        return True
    name = type(exc).__name__
    return name == "ResourceNotFoundException"


def _is_transient(exc: Exception) -> bool:
    code = _error_code(exc)
    if code in _TRANSIENT_AWS_CODES:
        return True
    name = type(exc).__name__
    if name in {"EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError"}:
        return True
    return isinstance(exc, (ConnectionError, TimeoutError))


__all__ = ["AWSSecretsManagerResolver"]
