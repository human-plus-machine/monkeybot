"""Tracing wrapper around :class:`SecretResolver` (Story 6).

Emits a ``secret.resolved`` :class:`HarnessEvent` on every **successful**
resolution. Failure paths never emit — ``SecretNotFound`` is the normal
shape of a composite probing its legs.

Critical invariant (SEC-C-04): the event payload contains only a
``blake2s`` digest of the handle, never the raw handle, and **never** the
resolved secret value.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import SecretStr

from ..base import SecretResolver

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...event_bus import EventBus


def _default_principal_id_source() -> str:
    """Look up the current principal id from :mod:`principal_propagation`."""
    try:
        from ...middleware.principal_propagation import current_principal
    except Exception:  # pragma: no cover - defensive import
        return ""
    try:
        return current_principal().id
    except Exception:  # pragma: no cover - ContextVar edge cases
        return ""


def _hash_handle(handle: str) -> str:
    """Return an 8-byte ``blake2s`` hex digest of ``handle``."""
    return hashlib.blake2s(handle.encode("utf-8"), digest_size=8).hexdigest()


class TracingResolver(SecretResolver):
    """Wrap an inner :class:`SecretResolver` and emit an audit event on success.

    Args:
        inner: The real resolver whose result is returned to callers.
        event_bus: Optional :class:`EventBus`. When supplied, a
            ``secret.resolved`` :class:`HarnessEvent` is published for every
            successful resolve. When omitted, callers can still inspect the
            emitted payloads via ``last_payload`` (used by unit tests).
        principal_id_source: Callable returning the current principal id
            (defaults to :func:`current_principal`).
    """

    def __init__(
        self,
        inner: SecretResolver,
        *,
        event_bus: EventBus | None = None,
        principal_id_source: Callable[[], str] = _default_principal_id_source,
    ) -> None:
        self.inner = inner
        self._event_bus = event_bus
        self._principal_id_source = principal_id_source
        self.last_payload: dict[str, object] | None = None

    async def resolve(self, handle: str) -> SecretStr:
        """Delegate to ``inner`` and emit ``secret.resolved`` on success.

        Failures (including :class:`SecretNotFound`) propagate verbatim and
        never produce an event.
        """
        start = time.monotonic()
        value = await self.inner.resolve(handle)
        latency_ms = int((time.monotonic() - start) * 1000)
        payload: dict[str, object] = {
            "handle_hash": _hash_handle(handle),
            "resolver": type(self.inner).__name__,
            "principal_id": self._principal_id_source() or "",
            "latency_ms": latency_ms,
        }
        self.last_payload = payload
        await self._publish(payload)
        return value

    async def _publish(self, payload: dict[str, object]) -> None:
        bus = self._event_bus
        if bus is None:
            return
        try:
            from ...events import EventKind, HarnessEvent, Principal, VersionTriple
            from ...middleware.principal_propagation import (
                current_principal,
                current_run_id,
                current_session_id,
            )
        except Exception:  # pragma: no cover - defensive
            return
        try:
            principal = current_principal()
            run_id = current_run_id()
            session_id = current_session_id()
        except Exception:  # pragma: no cover - defensive
            principal = Principal()
            run_id = "unset"
            session_id = "unset"
        event = HarnessEvent(
            run_id=run_id,
            session_id=session_id,
            principal=principal,
            versions=VersionTriple(harness="1", deep_agents="unknown", model="unknown"),
            ts=datetime.now(UTC),
            kind=EventKind.SECRET_RESOLVED,
            payload=payload,
        )
        await bus.publish(event)


__all__ = ["TracingResolver"]
