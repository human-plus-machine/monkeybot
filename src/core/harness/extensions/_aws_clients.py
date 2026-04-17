"""Shared async AWS SDK session + client factories.

Story 3 owns this module (first AWS-SDK consumer — ``S3MemoryStore``).
Stories 6 (``SecretResolver``) and 7 (``ModelProvider`` Bedrock) also import
from here. ``aioboto3`` is never imported at module top-level so this file
is safe to import without the optional dependency installed.

Sessions are memoised per ``region`` so callers targeting the same region
share a single :class:`aioboto3.Session`. Tests that need a fresh session
can call :func:`reset` during teardown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .errors import BackendConfigError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aioboto3 import Session

_SESSIONS: dict[str | None, Any] = {}


def _load_session_class() -> Any:
    """Lazily import :class:`aioboto3.Session`, raising a structured error on miss."""
    try:
        from aioboto3 import Session  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - optional dep
        raise BackendConfigError(
            "AWS backends require emonk[memory-store-s3] (aioboto3)"
        ) from exc
    return Session


def get_session(region: str | None = None) -> Session:
    """Return (creating once) the shared :class:`aioboto3.Session` for ``region``.

    Args:
        region: Optional AWS region name. ``None`` yields the SDK-default
            region (boto3's normal resolution chain).

    Returns:
        A memoised :class:`aioboto3.Session`.

    Raises:
        BackendConfigError: ``aioboto3`` is not installed.
    """
    if region not in _SESSIONS:
        session_cls = _load_session_class()
        _SESSIONS[region] = session_cls(region_name=region)
    return _SESSIONS[region]


def s3_client(region: str | None = None) -> Any:
    """Return an async S3 client context manager for ``region``."""
    return get_session(region).client("s3")


def secrets_client(region: str | None = None) -> Any:
    """Return an async Secrets Manager client context manager for ``region``."""
    return get_session(region).client("secretsmanager")


def bedrock_runtime(region: str | None = None) -> Any:
    """Return an async Bedrock Runtime client context manager for ``region``."""
    return get_session(region).client("bedrock-runtime")


def reset() -> None:
    """Clear the cached session table. Intended for test teardown."""
    _SESSIONS.clear()


__all__ = [
    "bedrock_runtime",
    "get_session",
    "reset",
    "s3_client",
    "secrets_client",
]
