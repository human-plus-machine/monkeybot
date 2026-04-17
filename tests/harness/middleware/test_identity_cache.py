"""Unit tests for :class:`IdentityCache` (Story 5).

Exercise LRU eviction, TTL expiry (via ``time.monotonic`` monkeypatching
to avoid real sleeps), and the ``invalidate(predicate)`` bust path.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.core.harness.middleware._identity_cache import IdentityCache


def test_cache_hit_miss_stats() -> None:
    """``get`` increments hits/misses and exposes them via :meth:`stats`."""
    cache = IdentityCache(capacity=4)
    assert cache.get(("alice", "s1")) is None
    cache.put(("alice", "s1"), "value-1", ttl_seconds=60)
    assert cache.get(("alice", "s1")) == "value-1"

    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["size"] == 1
    assert stats["hit_rate"] == pytest.approx(0.5)


def test_cache_lru_eviction_counts_capacity() -> None:
    """Inserting past capacity evicts the LRU entry and bumps the counter."""
    cache = IdentityCache(capacity=2)
    cache.put(("alice", "s"), 1, ttl_seconds=60)
    cache.put(("bob", "s"), 2, ttl_seconds=60)
    cache.put(("carol", "s"), 3, ttl_seconds=60)  # evicts ("alice", "s")

    assert cache.get(("alice", "s")) is None
    assert cache.get(("bob", "s")) == 2
    assert cache.stats()["evictions_capacity"] == 1


def test_cache_ttl_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entries past their TTL are evicted lazily on the next ``get``."""
    now = [1_000.0]
    monkeypatch.setattr(
        "src.core.harness.middleware._identity_cache.time.monotonic",
        lambda: now[0],
    )
    cache = IdentityCache(capacity=8)
    cache.put(("alice", "s"), "val", ttl_seconds=10)
    now[0] += 11.0

    assert cache.get(("alice", "s")) is None
    assert cache.stats()["evictions_ttl"] == 1


def test_cache_invalidate_with_predicate() -> None:
    """``invalidate`` evicts every key matching the predicate."""
    cache = IdentityCache(capacity=8)
    cache.put(("alice", "s1"), 1, ttl_seconds=60)
    cache.put(("alice", "s2"), 2, ttl_seconds=60)
    cache.put(("bob", "s1"), 3, ttl_seconds=60)

    def pick_alice(key: Any) -> bool:
        return isinstance(key, tuple) and key[0] == "alice"

    assert cache.invalidate(pick_alice) == 2
    assert cache.get(("alice", "s1")) is None
    assert cache.get(("bob", "s1")) == 3
    assert cache.stats()["evictions_bust"] == 2


def test_cache_move_to_end_on_hit() -> None:
    """A ``get`` promotes the key so it survives the next capacity eviction."""
    cache = IdentityCache(capacity=2)
    cache.put(("alice", "s"), 1, ttl_seconds=60)
    cache.put(("bob", "s"), 2, ttl_seconds=60)
    assert cache.get(("alice", "s")) == 1  # now alice is MRU
    cache.put(("carol", "s"), 3, ttl_seconds=60)  # evicts bob, not alice
    assert cache.get(("alice", "s")) == 1
    assert cache.get(("bob", "s")) is None


def test_cache_requires_positive_capacity() -> None:
    """Constructor rejects ``capacity <= 0``."""
    with pytest.raises(ValueError):
        IdentityCache(capacity=0)
