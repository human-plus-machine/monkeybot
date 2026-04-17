"""Firestore-backed :class:`MemoryStore` shipped as a builtin backend.

See 1b-contracts.md §3.2. Items live under a single collection keyed by a
deterministic ``"|".join(namespace) + "|" + key`` document id; namespace and
key are also stored as explicit fields to keep the list/search paths cheap.
The Firestore SDK is imported lazily so this module is safe to import
without ``google-cloud-firestore`` installed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..base import MemoryStore
from ..values import Item, MemoryStoreCapabilities

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langgraph.store.base import BaseStore

_NS_SEP = "|"


def _encode_doc_id(namespace: tuple[str, ...], key: str) -> str:
    """Return a collision-resistant document id from ``namespace`` + ``key``."""
    return _NS_SEP.join((*namespace, key))


class FirestoreMemoryStore(MemoryStore):
    """Firestore-backed ABC-conformant memory store.

    Args:
        project_id: GCP project id. ``None`` uses ADC defaults.
        collection: Name of the Firestore collection holding every item
            document. Defaults to ``"memory"``.
    """

    def __init__(
        self,
        *,
        project_id: str | None = None,
        collection: str = "memory",
    ) -> None:
        self.project_id = project_id
        self.collection = collection
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import firestore  # type: ignore[import-untyped]
            except ImportError as exc:  # pragma: no cover - optional dep
                raise RuntimeError(
                    "FirestoreMemoryStore requires emonk[firestore]"
                ) from exc
            self._client = (
                firestore.Client(project=self.project_id)
                if self.project_id
                else firestore.Client()
            )
        return self._client

    def _collection(self) -> Any:
        return self._get_client().collection(self.collection)

    async def put(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: Mapping[str, Any],
        *,
        ttl: timedelta | None = None,
    ) -> None:
        """Insert/overwrite the document for ``(namespace, key)`` with TTL support."""
        namespace = tuple(namespace)
        now = datetime.now(UTC)
        expires_at = now + ttl if ttl is not None else None

        def _write() -> None:
            doc_ref = self._collection().document(_encode_doc_id(namespace, key))
            snap = doc_ref.get()
            created_at = now
            if getattr(snap, "exists", False):
                existing = snap.to_dict() or {}
                created_at = existing.get("created_at", now)
            doc_ref.set(
                {
                    "namespace": list(namespace),
                    "key": key,
                    "value": dict(value),
                    "created_at": created_at,
                    "updated_at": now,
                    "expires_at": expires_at,
                }
            )

        await asyncio.to_thread(_write)

    async def get(self, namespace: tuple[str, ...], key: str) -> Item | None:
        """Return the :class:`Item` at ``(namespace, key)`` or ``None``."""
        namespace = tuple(namespace)

        def _read() -> dict[str, Any] | None:
            snap = self._collection().document(_encode_doc_id(namespace, key)).get()
            if not getattr(snap, "exists", False):
                return None
            return snap.to_dict() or {}

        data = await asyncio.to_thread(_read)
        if data is None:
            return None
        return self._row_to_item(data)

    async def search(
        self,
        namespace: tuple[str, ...],
        *,
        query: str | None = None,
        filter: Mapping[str, Any] | None = None,
        limit: int = 10,
    ) -> list[Item]:
        """Return live items under ``namespace`` matching filter/query."""
        namespace = tuple(namespace)

        def _stream() -> list[dict[str, Any]]:
            col = self._collection()
            ns_list = list(namespace)
            try:
                q = col.where("namespace", "==", ns_list)
            except AttributeError:  # pragma: no cover - defensive fallback
                q = col
            return [doc.to_dict() or {} for doc in q.stream()]

        rows = await asyncio.to_thread(_stream)
        out: list[Item] = []
        query_lower = query.lower() if query else None
        for data in rows:
            item = self._row_to_item(data)
            if item is None:
                continue
            if filter is not None and not all(
                item.value.get(fk) == fv for fk, fv in filter.items()
            ):
                continue
            if query_lower is not None and query_lower not in json.dumps(
                item.value, default=str
            ).lower():
                continue
            out.append(item)
            if len(out) >= limit:
                break
        return out

    async def delete(self, namespace: tuple[str, ...], key: str) -> None:
        """Delete the document at ``(namespace, key)`` if present."""
        namespace = tuple(namespace)

        def _delete() -> None:
            self._collection().document(_encode_doc_id(namespace, key)).delete()

        await asyncio.to_thread(_delete)

    async def list_namespaces(
        self, prefix: tuple[str, ...] = ()
    ) -> list[tuple[str, ...]]:
        """Return distinct namespaces under ``prefix`` (server-side scan)."""
        prefix = tuple(prefix)

        def _scan() -> list[tuple[str, ...]]:
            col = self._collection()
            seen: set[tuple[str, ...]] = set()
            for doc in col.stream():
                data = doc.to_dict() or {}
                expires_at = data.get("expires_at")
                if (
                    isinstance(expires_at, datetime)
                    and expires_at < datetime.now(UTC)
                ):
                    continue
                ns_raw = data.get("namespace")
                if not isinstance(ns_raw, list | tuple):
                    continue
                ns = tuple(str(part) for part in ns_raw)
                if len(ns) < len(prefix) or ns[: len(prefix)] != prefix:
                    continue
                seen.add(ns)
            return sorted(seen)

        return await asyncio.to_thread(_scan)

    def capabilities(self) -> MemoryStoreCapabilities:
        """Declared capabilities for a Firestore-backed store."""
        return MemoryStoreCapabilities(
            vector_search=False,
            keyword_search=True,
            namespace_listing=True,
            ttl=False,
            transactional=False,
        )

    def as_langgraph_store(self) -> BaseStore:
        """Return a LangGraph :class:`BaseStore` adapter bound to this store."""
        from ._langgraph_adapter import as_langgraph_store

        return as_langgraph_store(self)

    def _row_to_item(self, data: Mapping[str, Any]) -> Item | None:
        expires_at = data.get("expires_at")
        if (
            isinstance(expires_at, datetime)
            and expires_at < datetime.now(UTC)
        ):
            return None
        ns_raw = data.get("namespace") or ()
        namespace = tuple(str(part) for part in ns_raw)
        key = str(data.get("key", ""))
        value = data.get("value") or {}
        created_at = data.get("created_at") or datetime.now(UTC)
        updated_at = data.get("updated_at") or created_at
        return Item(
            value=dict(value),
            key=key,
            namespace=namespace,
            created_at=created_at,
            updated_at=updated_at,
        )


__all__ = ["FirestoreMemoryStore"]
