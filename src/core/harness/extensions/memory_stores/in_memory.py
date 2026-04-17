"""Process-local :class:`MemoryStore` — the reference backend.

Drop-in baseline for tests, local dev, and deterministic demos. Data is held
in a nested ``dict`` keyed by ``namespace`` and ``key``; TTL is tracked per
entry and enforced lazily on read. See 1b-contracts.md §3.2 and §11.1.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..base import MemoryStore
from ..values import Item, MemoryStoreCapabilities

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langgraph.store.base import BaseStore


@dataclass
class _Entry:
    """Internal record combining the stored :class:`Item` with its TTL deadline."""

    item: Item
    expires_at: datetime | None


class InMemoryMemoryStore(MemoryStore):
    """Asyncio-safe process-local :class:`MemoryStore` implementation.

    The store is ideal for tests and local prototyping. It supports keyword
    substring search (via ``json.dumps`` on the value), dict-subset filters,
    TTL expiry, and namespace listing.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, ...], dict[str, _Entry]] = {}
        self._lock = asyncio.Lock()

    async def put(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: Mapping[str, Any],
        *,
        ttl: timedelta | None = None,
    ) -> None:
        """Insert or overwrite ``(namespace, key)``; preserves ``created_at`` on update."""
        now = datetime.now(UTC)
        expires_at = now + ttl if ttl is not None else None
        namespace = tuple(namespace)
        async with self._lock:
            bucket = self._store.setdefault(namespace, {})
            existing = bucket.get(key)
            created_at = existing.item.created_at if existing is not None else now
            item = Item(
                value=dict(value),
                key=key,
                namespace=namespace,
                created_at=created_at,
                updated_at=now,
            )
            bucket[key] = _Entry(item=item, expires_at=expires_at)

    async def get(self, namespace: tuple[str, ...], key: str) -> Item | None:
        """Return the item at ``(namespace, key)`` or ``None`` if absent/expired."""
        namespace = tuple(namespace)
        async with self._lock:
            return self._live_item(namespace, key)

    async def search(
        self,
        namespace: tuple[str, ...],
        *,
        query: str | None = None,
        filter: Mapping[str, Any] | None = None,
        limit: int = 10,
    ) -> list[Item]:
        """Return items under ``namespace`` matching both ``filter`` and ``query``."""
        namespace = tuple(namespace)
        results: list[Item] = []
        async with self._lock:
            bucket = self._store.get(namespace, {})
            query_lower = query.lower() if query else None
            for key in list(bucket.keys()):
                item = self._live_item(namespace, key)
                if item is None:
                    continue
                if filter is not None and not all(
                    item.value.get(fk) == fv for fk, fv in filter.items()
                ):
                    continue
                if query_lower is not None:
                    haystack = json.dumps(item.value, default=str).lower()
                    if query_lower not in haystack:
                        continue
                results.append(item)
                if len(results) >= limit:
                    break
        return results

    async def delete(self, namespace: tuple[str, ...], key: str) -> None:
        """Delete the entry at ``(namespace, key)`` if present."""
        namespace = tuple(namespace)
        async with self._lock:
            bucket = self._store.get(namespace)
            if bucket is None:
                return
            bucket.pop(key, None)
            if not bucket:
                self._store.pop(namespace, None)

    async def list_namespaces(
        self, prefix: tuple[str, ...] = ()
    ) -> list[tuple[str, ...]]:
        """Return every namespace with ``prefix`` that currently holds live items."""
        prefix = tuple(prefix)
        async with self._lock:
            live: set[tuple[str, ...]] = set()
            for ns in list(self._store.keys()):
                if len(ns) < len(prefix) or ns[: len(prefix)] != prefix:
                    continue
                if any(
                    self._live_item(ns, key) is not None
                    for key in list(self._store[ns].keys())
                ):
                    live.add(ns)
        return sorted(live)

    def capabilities(self) -> MemoryStoreCapabilities:
        """Declared capabilities: keyword search, TTL, namespace listing; no vectors."""
        return MemoryStoreCapabilities(
            vector_search=False,
            keyword_search=True,
            namespace_listing=True,
            ttl=True,
            transactional=False,
        )

    def as_langgraph_store(self) -> BaseStore:
        """Return a LangGraph :class:`BaseStore` adapter bound to this store."""
        from ._langgraph_adapter import as_langgraph_store

        return as_langgraph_store(self)

    def _live_item(self, namespace: tuple[str, ...], key: str) -> Item | None:
        bucket = self._store.get(namespace)
        if bucket is None:
            return None
        entry = bucket.get(key)
        if entry is None:
            return None
        if entry.expires_at is not None and entry.expires_at < datetime.now(UTC):
            bucket.pop(key, None)
            if not bucket:
                self._store.pop(namespace, None)
            return None
        return entry.item


__all__ = ["InMemoryMemoryStore"]
