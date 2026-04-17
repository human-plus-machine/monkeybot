"""Mongo-backed :class:`MemoryStore` shipped as a builtin backend.

See 1b-contracts.md §3.2 and §8.2. A single collection holds every item;
indexes (compound unique on ``(namespace, key)``, TTL on ``expires_at``,
text index on ``value``) are created lazily on first use via
:meth:`_ensure_indexes`.

Motor is imported lazily inside :mod:`_mongo_client` — importing this
module is free of the optional dependency.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .._mongo_client import get_client
from ..base import MemoryStore
from ..values import Item, MemoryStoreCapabilities

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langgraph.store.base import BaseStore


class MongoMemoryStore(MemoryStore):
    """ABC-conformant :class:`MemoryStore` backed by MongoDB.

    Args:
        uri_env: Env var name holding the Mongo connection URI.
        database: Target database (default ``"emonk"``).
        collection: Target collection (default ``"memory"``).
    """

    def __init__(
        self,
        *,
        uri_env: str = "MONGO_URI",
        database: str = "emonk",
        collection: str = "memory",
    ) -> None:
        self.uri_env = uri_env
        self.database = database
        self.collection_name = collection
        self._collection: Any = None
        self._indexes_ready = False

    async def _ensure_collection(self) -> Any:
        if self._collection is not None:
            return self._collection
        client = await get_client(uri_env=self.uri_env)
        collection = client[self.database][self.collection_name]
        if not self._indexes_ready:
            await collection.create_index(
                [("namespace", 1), ("key", 1)], unique=True
            )
            await collection.create_index("expires_at", expireAfterSeconds=0)
            with contextlib.suppress(Exception):
                await collection.create_index([("value", "text")])
            self._indexes_ready = True
        self._collection = collection
        return collection

    async def put(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: Mapping[str, Any],
        *,
        ttl: timedelta | None = None,
    ) -> None:
        """Upsert the document keyed by ``(namespace, key)``."""
        namespace_list = list(namespace)
        now = datetime.now(UTC)
        expires_at = now + ttl if ttl is not None else None
        collection = await self._ensure_collection()
        existing = await collection.find_one(
            {"namespace": namespace_list, "key": key}, {"created_at": 1}
        )
        created_at = existing["created_at"] if existing else now
        await collection.update_one(
            {"namespace": namespace_list, "key": key},
            {
                "$set": {
                    "namespace": namespace_list,
                    "key": key,
                    "value": dict(value),
                    "created_at": created_at,
                    "updated_at": now,
                    "expires_at": expires_at,
                }
            },
            upsert=True,
        )

    async def get(self, namespace: tuple[str, ...], key: str) -> Item | None:
        """Return the live :class:`Item` at ``(namespace, key)`` or ``None``."""
        collection = await self._ensure_collection()
        doc = await collection.find_one(
            {"namespace": list(namespace), "key": key}
        )
        if doc is None:
            return None
        return _doc_to_item(doc)

    async def search(
        self,
        namespace: tuple[str, ...],
        *,
        query: str | None = None,
        filter: Mapping[str, Any] | None = None,
        limit: int = 10,
    ) -> list[Item]:
        """Return live docs under ``namespace`` matching ``filter`` / ``query``."""
        collection = await self._ensure_collection()
        criteria: dict[str, Any] = {"namespace": list(namespace)}
        criteria["$or"] = [
            {"expires_at": None},
            {"expires_at": {"$gt": datetime.now(UTC)}},
        ]
        if filter:
            for field, expected in filter.items():
                criteria[f"value.{field}"] = expected
        if query is not None:
            criteria["$text"] = {"$search": query}
        cursor = collection.find(criteria).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [_doc_to_item(doc) for doc in docs if _doc_to_item(doc) is not None]  # type: ignore[misc]

    async def delete(self, namespace: tuple[str, ...], key: str) -> None:
        """Remove the document at ``(namespace, key)``."""
        collection = await self._ensure_collection()
        await collection.delete_one({"namespace": list(namespace), "key": key})

    async def list_namespaces(
        self, prefix: tuple[str, ...] = ()
    ) -> list[tuple[str, ...]]:
        """Return distinct live namespaces beginning with ``prefix``."""
        collection = await self._ensure_collection()
        prefix_list = list(prefix)
        criteria: dict[str, Any] = {
            "$or": [
                {"expires_at": None},
                {"expires_at": {"$gt": datetime.now(UTC)}},
            ]
        }
        raw = await collection.distinct("namespace", criteria)
        results: set[tuple[str, ...]] = set()
        for ns_raw in raw:
            if not isinstance(ns_raw, list | tuple):
                continue
            ns = tuple(str(part) for part in ns_raw)
            if len(ns) < len(prefix_list) or list(ns[: len(prefix_list)]) != prefix_list:
                continue
            results.add(ns)
        return sorted(results)

    def capabilities(self) -> MemoryStoreCapabilities:
        """Declared capabilities for a Mongo-backed store."""
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


def _doc_to_item(doc: Mapping[str, Any]) -> Item | None:
    expires_at = doc.get("expires_at")
    if isinstance(expires_at, datetime) and expires_at < datetime.now(UTC):
        return None
    ns_raw = doc.get("namespace") or ()
    namespace = tuple(str(part) for part in ns_raw)
    value = doc.get("value") or {}
    created_at = doc.get("created_at") or datetime.now(UTC)
    updated_at = doc.get("updated_at") or created_at
    return Item(
        value=dict(value) if isinstance(value, Mapping) else {"value": value},
        key=str(doc.get("key", "")),
        namespace=namespace,
        created_at=created_at,
        updated_at=updated_at,
    )


__all__ = ["MongoMemoryStore"]
