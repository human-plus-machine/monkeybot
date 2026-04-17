"""S3-backed :class:`MemoryStore` shipped as a builtin backend.

See 1b-contracts.md §3.2 and §8.3 (object layout). Each value round-trips
through ``orjson`` and is written at
``{prefix}/{namespace[0]}/.../{namespace[n]}/{key}.json`` with optional SSE
headers. TTL is stored in S3 object metadata (ISO 8601) and enforced lazily
on read — callers that care about aggressive expiry should additionally
configure a bucket lifecycle rule.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .._aws_clients import s3_client
from ..base import MemoryStore
from ..values import Item, MemoryStoreCapabilities

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langgraph.store.base import BaseStore


def _namespace_prefix(root_prefix: str, namespace: tuple[str, ...]) -> str:
    parts = [p for p in (root_prefix, *namespace) if p]
    return "/".join(parts) + "/" if parts else ""


def _object_key(root_prefix: str, namespace: tuple[str, ...], key: str) -> str:
    return f"{_namespace_prefix(root_prefix, namespace)}{key}.json"


def _dumps(value: Mapping[str, Any]) -> bytes:
    import orjson

    return orjson.dumps(dict(value), default=str)


def _loads(payload: bytes) -> Any:
    import orjson

    return orjson.loads(payload)


class S3MemoryStore(MemoryStore):
    """Amazon S3-backed :class:`MemoryStore` implementation.

    Args:
        bucket: Target S3 bucket (required).
        prefix: Optional object-key prefix applied to every write.
        region: Optional AWS region name (defers to SDK default when ``None``).
        sse: Optional server-side encryption mode (``"AES256"`` or ``"aws:kms"``).
        kms_key_id: Optional KMS key id (required when ``sse="aws:kms"``).
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        region: str | None = None,
        sse: str | None = None,
        kms_key_id: str | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("S3MemoryStore requires a non-empty bucket name")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.region = region
        self.sse = sse
        self.kms_key_id = kms_key_id

    def _put_kwargs(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if self.sse:
            extra["ServerSideEncryption"] = self.sse
        if self.kms_key_id:
            extra["SSEKMSKeyId"] = self.kms_key_id
        return extra

    async def put(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: Mapping[str, Any],
        *,
        ttl: timedelta | None = None,
    ) -> None:
        """Upload the serialized value to S3 with updated metadata headers."""
        namespace = tuple(namespace)
        now = datetime.now(UTC)
        expires_at = now + ttl if ttl is not None else None
        object_key = _object_key(self.prefix, namespace, key)
        payload = _dumps(value)

        async with s3_client(self.region) as client:
            created_at = now
            with contextlib.suppress(Exception):
                head = await client.head_object(Bucket=self.bucket, Key=object_key)
                existing_md = head.get("Metadata", {}) or {}
                if existing_md.get("created_at"):
                    try:
                        created_at = datetime.fromisoformat(existing_md["created_at"])
                    except ValueError:
                        created_at = now
            metadata = {
                "namespace": "/".join(namespace),
                "key": key,
                "created_at": created_at.isoformat(),
                "updated_at": now.isoformat(),
            }
            if expires_at is not None:
                metadata["expires_at"] = expires_at.isoformat()
            await client.put_object(
                Bucket=self.bucket,
                Key=object_key,
                Body=payload,
                ContentType="application/json",
                Metadata=metadata,
                **self._put_kwargs(),
            )

    async def get(self, namespace: tuple[str, ...], key: str) -> Item | None:
        """Return the stored :class:`Item` at ``(namespace, key)`` or ``None``."""
        namespace = tuple(namespace)
        object_key = _object_key(self.prefix, namespace, key)

        async with s3_client(self.region) as client:
            try:
                response = await client.get_object(Bucket=self.bucket, Key=object_key)
            except Exception as exc:  # noqa: BLE001 - aioboto3 error shape varies
                if _is_not_found(exc):
                    return None
                raise
            metadata = response.get("Metadata", {}) or {}
            if _metadata_expired(metadata):
                return None
            body = await response["Body"].read()
            try:
                value = _loads(body)
            except Exception:  # noqa: BLE001
                return None
            return _build_item(namespace, key, value, metadata)

    async def search(
        self,
        namespace: tuple[str, ...],
        *,
        query: str | None = None,
        filter: Mapping[str, Any] | None = None,
        limit: int = 10,
    ) -> list[Item]:
        """Return live objects under ``namespace`` matching filter (S3 has no keyword idx)."""
        namespace = tuple(namespace)
        prefix_path = _namespace_prefix(self.prefix, namespace)

        async with s3_client(self.region) as client:
            paginator = client.get_paginator("list_objects_v2")
            items: list[Item] = []
            async for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix_path):
                for entry in page.get("Contents", []) or []:
                    s3_key = entry["Key"]
                    if not s3_key.endswith(".json"):
                        continue
                    rel = s3_key[len(prefix_path) :]
                    if "/" in rel:
                        continue
                    item_key = rel[:-5]
                    head = await client.head_object(Bucket=self.bucket, Key=s3_key)
                    metadata = head.get("Metadata", {}) or {}
                    if _metadata_expired(metadata):
                        continue
                    response = await client.get_object(Bucket=self.bucket, Key=s3_key)
                    body = await response["Body"].read()
                    try:
                        value = _loads(body)
                    except Exception:  # noqa: BLE001
                        continue
                    item = _build_item(namespace, item_key, value, metadata)
                    if filter is not None and not all(
                        item.value.get(fk) == fv for fk, fv in filter.items()
                    ):
                        continue
                    if query is not None:
                        import json

                        haystack = json.dumps(item.value, default=str).lower()
                        if query.lower() not in haystack:
                            continue
                    items.append(item)
                    if len(items) >= limit:
                        return items
            return items

    async def delete(self, namespace: tuple[str, ...], key: str) -> None:
        """Remove the S3 object at ``(namespace, key)`` if present."""
        namespace = tuple(namespace)
        object_key = _object_key(self.prefix, namespace, key)
        async with s3_client(self.region) as client:
            await client.delete_object(Bucket=self.bucket, Key=object_key)

    async def list_namespaces(
        self, prefix: tuple[str, ...] = ()
    ) -> list[tuple[str, ...]]:
        """Return distinct namespaces holding live objects under ``prefix``."""
        prefix = tuple(prefix)
        scan_prefix = _namespace_prefix(self.prefix, prefix)

        async with s3_client(self.region) as client:
            paginator = client.get_paginator("list_objects_v2")
            seen: set[tuple[str, ...]] = set()
            root_parts = [p for p in (self.prefix,) if p]
            async for page in paginator.paginate(Bucket=self.bucket, Prefix=scan_prefix):
                for entry in page.get("Contents", []) or []:
                    s3_key = entry["Key"]
                    if not s3_key.endswith(".json"):
                        continue
                    parts = s3_key.split("/")
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

    def capabilities(self) -> MemoryStoreCapabilities:
        """Declared capabilities: no keyword search on raw S3 (explicit per spec)."""
        return MemoryStoreCapabilities(
            vector_search=False,
            keyword_search=False,
            namespace_listing=True,
            ttl=False,
            transactional=False,
        )

    def as_langgraph_store(self) -> BaseStore:
        """Return a LangGraph :class:`BaseStore` adapter bound to this store."""
        from ._langgraph_adapter import as_langgraph_store

        return as_langgraph_store(self)


def _metadata_expired(metadata: Mapping[str, Any]) -> bool:
    raw = metadata.get("expires_at")
    if not raw:
        return False
    try:
        expires_at = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at < datetime.now(UTC)


def _build_item(
    namespace: tuple[str, ...],
    key: str,
    value: Any,
    metadata: Mapping[str, Any],
) -> Item:
    def _parse(ts: Any) -> datetime:
        if isinstance(ts, str):
            try:
                parsed = datetime.fromisoformat(ts)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                return datetime.now(UTC)
        return datetime.now(UTC)

    created_at = _parse(metadata.get("created_at"))
    updated_at = _parse(metadata.get("updated_at"))
    return Item(
        value=dict(value) if isinstance(value, Mapping) else {"value": value},
        key=key,
        namespace=namespace,
        created_at=created_at,
        updated_at=updated_at,
    )


def _is_not_found(exc: Exception) -> bool:
    if hasattr(exc, "response"):
        status = (
            getattr(exc, "response", {})
            .get("ResponseMetadata", {})
            .get("HTTPStatusCode")
        )
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        return status == 404 or code in {"NoSuchKey", "404", "NotFound"}
    name = type(exc).__name__
    return name in {"NoSuchKey", "ClientError"} and "404" in str(exc)


__all__ = ["S3MemoryStore"]
