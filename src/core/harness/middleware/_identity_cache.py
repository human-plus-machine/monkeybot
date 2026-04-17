"""LRU + TTL cache used by :class:`IdentityResolutionMW` (Story 5).

See 1b-contracts.md §7.2. The cache is deliberately minimal:

* O(1) ``get`` / ``put`` / ``invalidate(predicate)`` through an
  :class:`OrderedDict`.
* Soft TTL enforced on read (expired entries are evicted lazily and counted
  under ``evictions_ttl``).
* Capacity-bounded with LRU eviction (``evictions_capacity``).
* ``invalidate`` buckets evictions under ``evictions_bust``.
* Every eviction (TTL / capacity / bust) fires the optional ``on_evict``
  callback so :class:`IdentityResolutionMW` can publish
  ``identity.cache_evict`` / ``identity.bust`` events on the bus without the
  cache itself depending on the event module.

Stats are exposed via :meth:`stats` so the ``/harness/identity/cache/stats``
endpoint can surface them verbatim.
"""

from __future__ import annotations

import contextlib
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any


class IdentityCache:
    """Per-pipeline LRU + TTL cache keyed by ``(principal_id, session_id)``.

    Args:
        capacity: Maximum number of live entries before LRU eviction kicks
            in. Defaults to ``1024`` (matches :class:`IdentityResolutionMW`).
        on_evict: Optional callback ``fn(key, reason)`` invoked synchronously
            whenever the cache drops an entry. ``reason`` is ``"ttl"``,
            ``"capacity"``, or ``"bust"``. Errors raised by the callback are
            suppressed to preserve the cache's O(1) guarantees.
    """

    def __init__(
        self,
        capacity: int = 1024,
        *,
        on_evict: Callable[[Hashable, str], None] | None = None,
    ) -> None:
        if capacity <= 0:
            raise ValueError("IdentityCache capacity must be > 0")
        self._capacity = capacity
        self._d: OrderedDict[Hashable, tuple[Any, float]] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions_capacity = 0
        self._evictions_ttl = 0
        self._evictions_bust = 0
        self._on_evict = on_evict

    def _notify_evict(self, key: Hashable, reason: str) -> None:
        if self._on_evict is None:
            return
        with contextlib.suppress(Exception):
            self._on_evict(key, reason)

    @property
    def capacity(self) -> int:
        """Maximum number of live entries."""
        return self._capacity

    def __len__(self) -> int:
        return len(self._d)

    def get(self, key: Hashable) -> Any | None:
        """Return the cached value for ``key`` (touching LRU order) or ``None``."""
        entry = self._d.get(key)
        if entry is None:
            self._misses += 1
            return None
        value, expires_at = entry
        if time.monotonic() >= expires_at:
            del self._d[key]
            self._evictions_ttl += 1
            self._misses += 1
            self._notify_evict(key, "ttl")
            return None
        self._d.move_to_end(key)
        self._hits += 1
        return value

    def put(self, key: Hashable, value: Any, ttl_seconds: int) -> None:
        """Insert or refresh ``(key → value)`` with ``ttl_seconds`` soft TTL."""
        if key in self._d:
            self._d.move_to_end(key)
        self._d[key] = (value, time.monotonic() + max(1, int(ttl_seconds)))
        while len(self._d) > self._capacity:
            evicted_key, _ = self._d.popitem(last=False)
            self._evictions_capacity += 1
            self._notify_evict(evicted_key, "capacity")

    def invalidate(self, predicate: Callable[[Hashable], bool]) -> int:
        """Evict every key matching ``predicate``. Returns the eviction count."""
        to_evict = [k for k in self._d if predicate(k)]
        for k in to_evict:
            del self._d[k]
            self._notify_evict(k, "bust")
        self._evictions_bust += len(to_evict)
        return len(to_evict)

    def stats(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of counters + live size."""
        total = self._hits + self._misses
        return {
            "size": len(self._d),
            "capacity": self._capacity,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": (self._hits / total) if total else 0.0,
            "evictions_capacity": self._evictions_capacity,
            "evictions_ttl": self._evictions_ttl,
            "evictions_bust": self._evictions_bust,
        }


__all__ = ["IdentityCache"]
