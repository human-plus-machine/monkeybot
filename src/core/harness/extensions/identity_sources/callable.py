"""Callable-backed :class:`IdentitySource` (Story 5).

Wraps an arbitrary ``async`` callable ``(principal, session_id) -> LoadedIdentity``
behind an :func:`asyncio.wait_for` timeout. Primarily used for tests and
runtime-only adapters where full backend plumbing is overkill. Does not
support ``write_memory``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..base import IdentitySource
from ..errors import IdentityNotFound
from ..values import LoadedIdentity, MemoryPatch

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...events import Principal


IdentityFn = Callable[["Principal", "str | None"], Awaitable[LoadedIdentity]]


class CallableIdentitySource(IdentitySource):
    """Delegate identity resolution to an awaitable function.

    Args:
        fn: Awaitable that accepts ``(principal, session_id)`` and returns a
            :class:`LoadedIdentity`. Raise :class:`IdentityNotFound` to signal
            a missing principal.
        timeout_seconds: Per-call timeout. :class:`asyncio.TimeoutError` is
            translated into :class:`IdentityNotFound` so upstream middleware
            can route the refusal through its normal error path.
        cache_ttl_seconds: TTL advertised on any identity this source does
            not set explicitly.
    """

    def __init__(
        self,
        fn: IdentityFn,
        *,
        timeout_seconds: float = 5.0,
        cache_ttl_seconds: int = 300,
    ) -> None:
        if not callable(fn):
            raise TypeError("CallableIdentitySource requires an async callable")
        self._fn = fn
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds

    async def load(
        self,
        *,
        principal: Principal,
        session_id: str | None = None,
    ) -> LoadedIdentity:
        """Run ``fn(principal, session_id)`` under a timeout."""
        try:
            return await asyncio.wait_for(
                self._fn(principal, session_id),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:  # noqa: UP041
            raise IdentityNotFound(principal.id) from exc

    async def write_memory(
        self,
        *,
        principal: Principal,  # noqa: ARG002
        patch: MemoryPatch,  # noqa: ARG002
    ) -> None:
        """Callable sources are read-only."""
        raise NotImplementedError(
            "CallableIdentitySource does not support write_memory"
        )


__all__ = ["CallableIdentitySource", "IdentityFn"]
