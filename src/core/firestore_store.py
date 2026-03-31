"""Firestore-backed LangGraph Store for long-term memory.

Drop-in replacement for GCSStore — implements the same langgraph.store.base.BaseStore
interface, backed by Firestore instead of GCS.

Document structure:
    {collection_prefix}_store/
        {namespace_0}/{namespace_1}/.../items/
            {key}   →  { value: {...}, updated_at: Timestamp }

Reuses the google-cloud-firestore SDK already present in monkey-bot for
FirestoreCheckpointSaver and FirestoreStorage (no new dependency needed).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from langgraph.store.base import BaseStore, Item

logger = logging.getLogger(__name__)


def _namespace_to_collection(namespace: tuple, collection_prefix: str) -> str:
    """Convert a namespace tuple to a Firestore collection path.

    Firestore requires alternating collection/document segments. We map:
        ("shared", "session_summaries")
        → "{prefix}_ns/shared/session_summaries/items"

    The top-level collection is `{prefix}_ns`, with document segments for each
    namespace component, and a final `items` subcollection for the actual data.
    """
    if not namespace:
        return f"{collection_prefix}_ns/root/items"
    parts = list(namespace)
    # Build: collection / doc / collection / doc ... / items
    # Firestore requires alternating collection–document pairs.
    # Strategy: nest each namespace part as doc_id inside a subcollection named the same thing.
    # e.g. ("shared", "session_summaries")
    #   → prefix_ns / shared / shared / session_summaries / items
    # Simpler: encode the whole namespace as a single document path separator.
    # We use a flat top-level collection keyed by joined namespace + an "items" subcollection.
    joined = "__".join(parts)
    return f"{collection_prefix}_ns/{joined}/items"


class FirestoreStore(BaseStore):
    """Firestore-backed Store for LangGraph long-term memory.

    Stores JSON documents in Firestore with namespace-based organisation.
    Reuses ADC credentials — same service account as Vertex AI and Firestore checkpoints.

    Example:
        >>> store = FirestoreStore(project_id="aurigaos", collection_prefix="marketing-memory")
        >>>
        >>> store.put(
        ...     namespace=("shared", "session_summaries"),
        ...     key="thread-456",
        ...     value={"summary": "Discussed campaign goals", "key_topics": ["brand", "ads"]}
        ... )
        >>>
        >>> results = store.search(
        ...     namespace=("shared", "session_summaries"),
        ...     query="campaign"
        ... )
    """

    def __init__(
        self,
        project_id: str,
        collection_prefix: str = "memory",
    ) -> None:
        """
        Args:
            project_id:         GCP project ID
            collection_prefix:  Prefix for Firestore collection names (e.g. "marketing-memory")
        """
        self.project_id = project_id
        self.collection_prefix = collection_prefix
        self._db = None
        logger.info(
            "FirestoreStore configured",
            extra={"project_id": project_id, "collection_prefix": collection_prefix},
        )

    @property
    def db(self):
        """Lazy-initialize the synchronous Firestore client."""
        if self._db is None:
            from google.cloud import firestore  # noqa: PLC0415
            self._db = firestore.Client(project=self.project_id)
        return self._db

    def _items_collection(self, namespace: tuple):
        """Return the Firestore CollectionReference for a namespace."""
        path = _namespace_to_collection(namespace, self.collection_prefix)
        # path is like "prefix_ns/joined/items" — walk it as alternating collection/doc/collection
        segments = path.split("/")
        ref = self.db.collection(segments[0])
        for i, seg in enumerate(segments[1:], 1):
            if i % 2 == 1:
                ref = ref.document(seg)
            else:
                ref = ref.collection(seg)
        return ref

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def put(self, namespace: tuple, key: str, value: dict[str, Any]) -> None:
        """Store a document in Firestore."""
        from google.cloud.firestore_v1 import SERVER_TIMESTAMP  # noqa: PLC0415

        col = self._items_collection(namespace)
        col.document(key).set({"value": value, "updated_at": SERVER_TIMESTAMP})
        logger.debug("FirestoreStore: put %s/%s", namespace, key)

    def get(self, namespace: tuple, key: str) -> Item | None:
        """Retrieve a document from Firestore."""
        col = self._items_collection(namespace)
        doc = col.document(key).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        updated = data.get("updated_at")
        updated_str = updated.isoformat() if hasattr(updated, "isoformat") else None
        return Item(
            value=data["value"],
            key=key,
            namespace=namespace,
            created_at=updated_str,
            updated_at=updated_str,
        )

    def delete(self, namespace: tuple, key: str) -> None:
        """Delete a document from Firestore."""
        col = self._items_collection(namespace)
        col.document(key).delete()
        logger.debug("FirestoreStore: deleted %s/%s", namespace, key)

    def list(self, namespace: tuple, limit: int | None = None) -> list[Item]:
        """List all documents in a namespace."""
        col = self._items_collection(namespace)
        query = col.limit(limit) if limit else col
        items = []
        for doc in query.stream():
            data = doc.to_dict()
            if not data or "value" not in data:
                continue
            updated = data.get("updated_at")
            updated_str = updated.isoformat() if hasattr(updated, "isoformat") else None
            items.append(
                Item(
                    value=data["value"],
                    key=doc.id,
                    namespace=namespace,
                    created_at=updated_str,
                    updated_at=updated_str,
                )
            )
        return items

    def search(
        self,
        namespace: tuple,
        query: str | None = None,
        filter: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> list[Item]:
        """Search documents by keyword or filter.

        Keyword matching is performed on the full JSON serialisation of each
        document's value — same behaviour as GCSStore.search().
        """
        all_items = self.list(namespace)
        if not all_items:
            return []

        filtered = all_items

        if filter:
            filtered = [
                item for item in filtered
                if all(item.value.get(k) == v for k, v in filter.items())
            ]

        if query:
            query_lower = query.lower()
            scored = []
            for item in filtered:
                text = json.dumps(item.value).lower()
                score = text.count(query_lower)
                topics = item.value.get("key_topics", [])
                if isinstance(topics, list):
                    for topic in topics:
                        if query_lower in str(topic).lower():
                            score += 10
                if score > 0:
                    scored.append((score, item))
            scored.sort(reverse=True, key=lambda x: x[0])
            filtered = [item for _, item in scored]

        return filtered[:limit]

    # ------------------------------------------------------------------
    # LangGraph BaseStore ABC — batch / abatch
    # ------------------------------------------------------------------

    def batch(self, ops) -> list:
        """Execute multiple operations synchronously."""
        from langgraph.store.base import GetOp, ListNamespacesOp, PutOp, SearchOp  # noqa: PLC0415

        results = []
        for op in ops:
            if isinstance(op, GetOp):
                results.append(self.get(op.namespace, op.key))
            elif isinstance(op, PutOp):
                if op.value is None:
                    self.delete(op.namespace, op.key)
                else:
                    self.put(op.namespace, op.key, op.value)
                results.append(None)
            elif isinstance(op, SearchOp):
                results.append(
                    self.search(
                        op.namespace_prefix,
                        query=op.query,
                        filter=op.filter,
                        limit=op.limit,
                    )
                )
            elif isinstance(op, ListNamespacesOp):
                results.append(self._list_namespaces(op))
            else:
                results.append(None)
        return results

    async def abatch(self, ops) -> list:
        """Execute multiple operations asynchronously (runs sync batch in executor)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.batch, list(ops))

    def _list_namespaces(self, op) -> list:
        """Return unique namespace tuples stored under collection_prefix."""
        top_col = self.db.collection(f"{self.collection_prefix}_ns")
        seen: set[tuple] = set()
        for doc in top_col.stream():
            raw = doc.id  # e.g. "shared__session_summaries"
            parts = tuple(raw.split("__"))
            depth = op.max_depth
            namespace = parts[:depth] if depth else parts
            seen.add(namespace)
        namespaces = sorted(seen)
        offset = op.offset or 0
        limit = op.limit or 100
        return namespaces[offset : offset + limit]
