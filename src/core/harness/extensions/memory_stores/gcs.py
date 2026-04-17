"""GCS-backed :class:`MemoryStore` shipped as a builtin backend.

See 1b-contracts.md §3.2 and §8.3 (object layout). Objects live under
``{prefix}/{namespace[0]}/.../{namespace[n]}/{key}.json``; object metadata
carries the Unix millisecond ``expires_at`` (if any) so TTL-equipped
deployments can opportunistically garbage-collect with Object Lifecycle
Management. The ``google.cloud.storage`` client is imported lazily.
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


def _namespace_prefix(root_prefix: str, namespace: tuple[str, ...]) -> str:
    parts = [p for p in (root_prefix, *namespace) if p]
    return "/".join(parts) + "/" if parts else ""


def _blob_name(root_prefix: str, namespace: tuple[str, ...], key: str) -> str:
    return f"{_namespace_prefix(root_prefix, namespace)}{key}.json"


class GCSMemoryStore(MemoryStore):
    """Google Cloud Storage-backed :class:`MemoryStore` implementation.

    Args:
        bucket: GCS bucket name (required).
        prefix: Optional key prefix applied to every object.
        project_id: Optional GCP project id. ``None`` defers to ADC.
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        project_id: str | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("GCSMemoryStore requires a non-empty bucket name")
        self.bucket_name = bucket
        self.prefix = prefix.strip("/")
        self.project_id = project_id
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import storage  # type: ignore[import-untyped]
            except ImportError as exc:  # pragma: no cover - optional dep
                raise RuntimeError(
                    "GCSMemoryStore requires emonk[gcs]"
                ) from exc
            self._client = storage.Client(project=self.project_id)
        return self._client

    def _bucket(self) -> Any:
        return self._get_client().bucket(self.bucket_name)

    async def put(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: Mapping[str, Any],
        *,
        ttl: timedelta | None = None,
    ) -> None:
        """Write the ``(namespace, key)`` object as JSON with optional TTL metadata."""
        namespace = tuple(namespace)
        now = datetime.now(UTC)
        expires_at = now + ttl if ttl is not None else None

        def _write() -> None:
            blob = self._bucket().blob(_blob_name(self.prefix, namespace, key))
            existing_created_at = now.isoformat()
            if blob.exists():
                existing_md = dict(blob.metadata or {})
                existing_created_at = existing_md.get("created_at", existing_created_at)
            metadata = {
                "namespace": "/".join(namespace),
                "key": key,
                "created_at": existing_created_at,
                "updated_at": now.isoformat(),
            }
            if expires_at is not None:
                metadata["expires_at"] = expires_at.isoformat()
            blob.metadata = metadata
            blob.upload_from_string(
                json.dumps(dict(value), default=str),
                content_type="application/json",
            )

        await asyncio.to_thread(_write)

    async def get(self, namespace: tuple[str, ...], key: str) -> Item | None:
        """Return the :class:`Item` stored under ``(namespace, key)`` or ``None``."""
        namespace = tuple(namespace)

        def _read() -> Item | None:
            blob = self._bucket().blob(_blob_name(self.prefix, namespace, key))
            if not blob.exists():
                return None
            blob.reload()
            metadata = dict(blob.metadata or {})
            if _is_expired(metadata):
                return None
            payload = blob.download_as_text()
            try:
                value = json.loads(payload)
            except json.JSONDecodeError:
                return None
            return _metadata_to_item(namespace, key, value, metadata, blob)

        return await asyncio.to_thread(_read)

    async def search(
        self,
        namespace: tuple[str, ...],
        *,
        query: str | None = None,
        filter: Mapping[str, Any] | None = None,
        limit: int = 10,
    ) -> list[Item]:
        """Return live objects under ``namespace`` matching filter/query."""
        namespace = tuple(namespace)
        prefix_path = _namespace_prefix(self.prefix, namespace)

        def _stream() -> list[Item]:
            client = self._get_client()
            blobs = client.list_blobs(self.bucket_name, prefix=prefix_path)
            items: list[Item] = []
            for blob in blobs:
                if not blob.name.endswith(".json"):
                    continue
                rel = blob.name[len(prefix_path) :]
                if "/" in rel:
                    continue
                key = rel[:-5]
                metadata = dict(blob.metadata or {})
                if _is_expired(metadata):
                    continue
                try:
                    value = json.loads(blob.download_as_text())
                except json.JSONDecodeError:
                    continue
                item = _metadata_to_item(namespace, key, value, metadata, blob)
                if filter is not None and not all(
                    item.value.get(fk) == fv for fk, fv in filter.items()
                ):
                    continue
                if query is not None:
                    haystack = json.dumps(item.value, default=str).lower()
                    if query.lower() not in haystack:
                        continue
                items.append(item)
                if len(items) >= limit:
                    break
            return items

        return await asyncio.to_thread(_stream)

    async def delete(self, namespace: tuple[str, ...], key: str) -> None:
        """Delete the object at ``(namespace, key)`` if it exists."""
        namespace = tuple(namespace)

        def _delete() -> None:
            blob = self._bucket().blob(_blob_name(self.prefix, namespace, key))
            if blob.exists():
                blob.delete()

        await asyncio.to_thread(_delete)

    async def list_namespaces(
        self, prefix: tuple[str, ...] = ()
    ) -> list[tuple[str, ...]]:
        """Return distinct namespaces containing live objects under ``prefix``."""
        prefix = tuple(prefix)
        scan_prefix = _namespace_prefix(self.prefix, prefix)

        def _scan() -> list[tuple[str, ...]]:
            client = self._get_client()
            blobs = client.list_blobs(self.bucket_name, prefix=scan_prefix)
            seen: set[tuple[str, ...]] = set()
            root_parts = [p for p in (self.prefix,) if p]
            for blob in blobs:
                if not blob.name.endswith(".json"):
                    continue
                metadata = dict(blob.metadata or {})
                if _is_expired(metadata):
                    continue
                parts = blob.name.split("/")
                if root_parts and parts[: len(root_parts)] == root_parts:
                    parts = parts[len(root_parts) :]
                namespace_parts = parts[:-1]
                if not namespace_parts:
                    continue
                ns = tuple(namespace_parts)
                if len(ns) < len(prefix) or ns[: len(prefix)] != prefix:
                    continue
                seen.add(ns)
            return sorted(seen)

        return await asyncio.to_thread(_scan)

    def capabilities(self) -> MemoryStoreCapabilities:
        """Declared capabilities for a GCS-backed object store."""
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


def _is_expired(metadata: Mapping[str, Any]) -> bool:
    exp = metadata.get("expires_at")
    if not exp:
        return False
    try:
        expires_at = datetime.fromisoformat(str(exp))
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at < datetime.now(UTC)


def _metadata_to_item(
    namespace: tuple[str, ...],
    key: str,
    value: Any,
    metadata: Mapping[str, Any],
    blob: Any,
) -> Item:
    def _parse(ts: Any) -> datetime:
        if isinstance(ts, datetime):
            return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
        if isinstance(ts, str):
            try:
                parsed = datetime.fromisoformat(ts)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                return datetime.now(UTC)
        return datetime.now(UTC)

    created_at = _parse(metadata.get("created_at") or getattr(blob, "time_created", None))
    updated_at = _parse(metadata.get("updated_at") or getattr(blob, "updated", None))
    return Item(
        value=dict(value) if isinstance(value, Mapping) else {"value": value},
        key=key,
        namespace=namespace,
        created_at=created_at,
        updated_at=updated_at,
    )


__all__ = ["GCSMemoryStore"]
