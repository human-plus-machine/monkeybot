"""IdentityResolutionMW — per-invocation identity resolution.

See 1b-contracts.md §§5.1 and 7.1-7.3. The middleware sits between
:class:`PrincipalPropagationMW` and :class:`RulesEnforcementMW` and is
responsible for turning the invocation-scoped ``Principal`` into a
:class:`LoadedIdentity` before any downstream policy middleware runs.

Key behaviours:

* **Override path**: When ``ctx["identity_override"]`` is populated by the
  caller (tests, admin tooling) the source is never consulted.
* **Cache**: Hits/misses go through :class:`IdentityCache`; per-key TTL is
  clamped to ``default_ttl_seconds`` to avoid stale unbounded caching.
* **Single-flight (R-16)**: Concurrent cold misses for the same key collapse
  to a single backend ``load`` call — the first arriver wins, every other
  arriver awaits its :class:`asyncio.Event` and reuses the result. Prevents
  stampedes against the identity source.
* **Retry**: A single retry is attempted on transient backend errors
  (:class:`BackendConfigError` or :class:`asyncio.TimeoutError`).
  :class:`IdentityNotFound` is NOT retried.
* **Events**: Every identity-flow event (``identity.load`` /
  ``identity.load_failed`` / ``identity.cache_evict`` / ``identity.bust``) is
  published through the configured :class:`EventBus` when one is wired in.
  Telemetry failures never break the hot path — emission is wrapped in
  :func:`contextlib.suppress`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..events import EventKind, HarnessEvent, Principal, VersionTriple
from ..extensions.errors import BackendConfigError, IdentityNotFound
from ._identity_cache import IdentityCache

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..event_bus import EventBus
    from ..extensions.base import IdentitySource

log = logging.getLogger("emonk.harness.identity_resolution")


class IdentityResolutionMW:
    """Async middleware that resolves identity for every invocation.

    Args:
        source: The :class:`IdentitySource` to consult on cache misses.
        cache_size: LRU capacity for the internal :class:`IdentityCache`.
        default_ttl_seconds: Upper bound on cache TTL (also the fallback when
            a loaded identity does not advertise its own TTL).
    """

    name = "IdentityResolutionMW"

    def __init__(
        self,
        source: IdentitySource,
        *,
        cache_size: int = 1024,
        default_ttl_seconds: int = 300,
        event_bus: EventBus | None = None,
        versions: VersionTriple | None = None,
    ) -> None:
        self.source = source
        self.cache = IdentityCache(cache_size, on_evict=self._on_cache_evict)
        self.default_ttl = default_ttl_seconds
        self.event_bus = event_bus
        self.versions = versions or VersionTriple(
            harness="unknown", deep_agents="unknown", model="unknown"
        )
        self._inflight: dict[tuple[str, str], asyncio.Future[Any]] = {}

    async def before(self, state: Any, ctx: dict[str, Any]) -> Any:
        """Populate ``ctx["identity"]`` from override → cache → source.

        Raises:
            IdentityNotFound: Propagated after the retry budget is exhausted.
        """
        override = ctx.get("identity_override")
        if override is not None:
            ctx["identity"] = override
            return state

        principal = ctx["principal"]
        session_id = ctx.get("session_id")
        run_id = ctx.get("run_id", "identity-mw")
        key = (principal.id, session_id or "__no_session__")

        cached = self.cache.get(key)
        if cached is not None:
            ctx["identity"] = cached
            self._publish(
                EventKind.IDENTITY_LOAD,
                principal=principal,
                session_id=session_id or "__no_session__",
                run_id=run_id,
                payload={
                    "principal_id": principal.id,
                    "session_id": session_id,
                    "cache_hit": True,
                    "latency_ms": 0,
                    "backend": type(self.source).__name__,
                },
            )
            return state

        inflight = self._inflight.get(key)
        if inflight is not None:
            try:
                identity = await inflight
            except IdentityNotFound as exc:
                self._publish(
                    EventKind.IDENTITY_LOAD_FAILED,
                    principal=principal,
                    session_id=session_id or "__no_session__",
                    run_id=run_id,
                    payload={
                        "principal_id": principal.id,
                        "session_id": session_id,
                        "error_class": type(exc).__name__,
                        "single_flight": "waiter",
                    },
                )
                raise
            ctx["identity"] = identity
            self._publish(
                EventKind.IDENTITY_LOAD,
                principal=principal,
                session_id=session_id or "__no_session__",
                run_id=run_id,
                payload={
                    "principal_id": principal.id,
                    "session_id": session_id,
                    "cache_hit": False,
                    "single_flight": "waiter",
                    "latency_ms": 0,
                    "backend": type(self.source).__name__,
                },
            )
            return state

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._inflight[key] = future

        start = time.monotonic()
        try:
            identity = await self._load_with_retry(principal, session_id)
        except IdentityNotFound as exc:
            if not future.done():
                future.set_exception(exc)
            self._publish(
                EventKind.IDENTITY_LOAD_FAILED,
                principal=principal,
                session_id=session_id or "__no_session__",
                run_id=run_id,
                payload={
                    "principal_id": principal.id,
                    "session_id": session_id,
                    "error_class": type(exc).__name__,
                },
            )
            raise
        except BaseException as exc:  # pragma: no cover - defensive
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            self._inflight.pop(key, None)

        if not future.done():
            future.set_result(identity)

        ttl_raw = getattr(identity, "ttl_seconds", None) or self.default_ttl
        ttl = min(int(ttl_raw), self.default_ttl)
        self.cache.put(key, identity, ttl)
        ctx["identity"] = identity
        self._publish(
            EventKind.IDENTITY_LOAD,
            principal=principal,
            session_id=session_id or "__no_session__",
            run_id=run_id,
            payload={
                "principal_id": principal.id,
                "session_id": session_id,
                "cache_hit": False,
                "single_flight": "leader",
                "latency_ms": int((time.monotonic() - start) * 1000),
                "backend": type(self.source).__name__,
            },
        )
        return state

    async def after(self, state: Any, ctx: dict[str, Any]) -> Any:
        """No-op post-hook — identity is invocation-scoped only."""
        return state

    async def _load_with_retry(
        self,
        principal: Any,
        session_id: str | None,
    ) -> Any:
        """Call the source with a single retry on transient failures."""
        try:
            return await self.source.load(principal=principal, session_id=session_id)
        except (TimeoutError, BackendConfigError):
            await asyncio.sleep(0.1)
            return await self.source.load(principal=principal, session_id=session_id)

    def _on_cache_evict(self, key: Any, reason: str) -> None:
        """Publish an ``identity.cache_evict`` event when the cache drops an entry.

        Called synchronously from :class:`IdentityCache`. ``reason`` is one of
        ``"ttl"``, ``"capacity"``, or ``"bust"``. Bust evictions are also
        emitted here so the ``/harness/identity/bust`` HTTP path doesn't have
        to duplicate the wiring.
        """
        principal_id = str(key[0]) if isinstance(key, tuple) else str(key)
        session_id = (
            str(key[1]) if isinstance(key, tuple) and len(key) > 1 else "__no_session__"
        )
        kind = EventKind.IDENTITY_BUST if reason == "bust" else EventKind.IDENTITY_CACHE_EVICT
        self._publish(
            kind,
            principal=Principal(kind="service", id=principal_id),
            session_id=session_id,
            run_id="identity-cache",
            payload={
                "principal_id": principal_id,
                "session_id": None if session_id == "__no_session__" else session_id,
                "reason": reason,
            },
        )

    def _publish(
        self,
        kind: EventKind,
        *,
        principal: Any,
        session_id: str,
        run_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Publish a :class:`HarnessEvent`; falls back to logger when bus absent."""
        if self.event_bus is None:
            log.info("harness.event kind=%s payload=%s", kind.value, payload)
            return
        event = HarnessEvent(
            run_id=run_id,
            session_id=session_id,
            principal=principal if isinstance(principal, Principal) else Principal(),
            versions=self.versions,
            ts=datetime.now(UTC),
            kind=kind,
            payload=payload,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.info("harness.event kind=%s payload=%s (no event loop)", kind.value, payload)
            return
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            loop.create_task(self.event_bus.publish(event))


__all__ = ["IdentityResolutionMW"]
