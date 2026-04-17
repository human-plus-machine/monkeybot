"""Unit tests for :class:`GCSMemoryStore` using an in-process fake GCS client.

The fake replicates only the attributes the store accesses
(``bucket``, ``blob``, ``upload_from_string``, ``download_as_text``,
``exists``, ``delete``, ``list_blobs``, ``metadata``, ``reload``). This
keeps the test lightweight and independent of ``google-cloud-storage``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from src.core.harness.extensions.memory_stores import GCSMemoryStore

pytestmark = pytest.mark.asyncio


class _FakeBlob:
    def __init__(self, bucket: _FakeBucket, name: str) -> None:
        self._bucket = bucket
        self.name = name
        self.metadata: dict[str, str] | None = None
        self._content: str | None = None
        self.time_created: datetime | None = None
        self.updated: datetime | None = None

    def upload_from_string(self, data: str, content_type: str | None = None) -> None:
        now = datetime.now(UTC)
        if self.time_created is None:
            self.time_created = now
        self.updated = now
        self._content = data
        self._bucket._blobs[self.name] = self

    def download_as_text(self) -> str:
        if self._content is None:
            raise FileNotFoundError(self.name)
        return self._content

    def exists(self) -> bool:
        return self.name in self._bucket._blobs

    def delete(self) -> None:
        self._bucket._blobs.pop(self.name, None)

    def reload(self) -> None:
        pass


class _FakeBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self._blobs: dict[str, _FakeBlob] = {}

    def blob(self, name: str) -> _FakeBlob:
        existing = self._blobs.get(name)
        if existing is not None:
            return existing
        return _FakeBlob(self, name)


class _FakeGCSClient:
    def __init__(self) -> None:
        self._buckets: dict[str, _FakeBucket] = {}

    def bucket(self, name: str) -> _FakeBucket:
        return self._buckets.setdefault(name, _FakeBucket(name))

    def list_blobs(
        self,
        bucket_name: str,
        prefix: str = "",
        max_results: int | None = None,
    ) -> list[_FakeBlob]:
        bucket = self._buckets.setdefault(bucket_name, _FakeBucket(bucket_name))
        matches = [
            blob
            for name, blob in bucket._blobs.items()
            if name.startswith(prefix)
        ]
        if max_results is not None:
            matches = matches[:max_results]
        return matches


@pytest.fixture
def store() -> GCSMemoryStore:
    backend = GCSMemoryStore(bucket="test-bucket", prefix="memory")
    backend._client = _FakeGCSClient()  # type: ignore[attr-defined]
    return backend


async def test_mem_c_01_put_then_get(store: GCSMemoryStore) -> None:
    """MEM-C-01: put + get returns identical value."""
    await store.put(("u", "1"), "k", {"v": 1})
    item = await store.get(("u", "1"), "k")
    assert item is not None
    assert item.value == {"v": 1}
    assert item.namespace == ("u", "1")


async def test_mem_c_02_overwrite_preserves_created_at(
    store: GCSMemoryStore,
) -> None:
    """MEM-C-02: overwrite preserves ``created_at``."""
    await store.put(("u",), "k", {"v": 1})
    first = await store.get(("u",), "k")
    assert first is not None
    await store.put(("u",), "k", {"v": 2})
    second = await store.get(("u",), "k")
    assert second is not None
    assert second.created_at == first.created_at
    assert second.value == {"v": 2}


async def test_mem_c_03_delete_then_get(store: GCSMemoryStore) -> None:
    """MEM-C-03: delete followed by get returns None."""
    await store.put(("u",), "k", {"v": 1})
    await store.delete(("u",), "k")
    assert await store.get(("u",), "k") is None


async def test_mem_c_04_search_honors_filter(store: GCSMemoryStore) -> None:
    """MEM-C-04: search honors dict-subset filter."""
    await store.put(("u",), "a", {"kind": "note"})
    await store.put(("u",), "b", {"kind": "task"})
    await store.put(("u",), "c", {"kind": "note"})
    hits = await store.search(("u",), filter={"kind": "note"}, limit=10)
    assert {item.key for item in hits} == {"a", "c"}


async def test_mem_c_05_list_namespaces_prefix(store: GCSMemoryStore) -> None:
    """MEM-C-05: list_namespaces honors prefix filter."""
    await store.put(("a", "1"), "k", {"v": 1})
    await store.put(("a", "2"), "k", {"v": 2})
    await store.put(("b", "1"), "k", {"v": 3})
    scoped = await store.list_namespaces(("a",))
    assert ("a", "1") in scoped
    assert ("a", "2") in scoped
    assert ("b", "1") not in scoped


async def test_mem_c_06_capabilities(store: GCSMemoryStore) -> None:
    """MEM-C-06: declared capabilities match the GCS backend behaviour."""
    caps = store.capabilities()
    assert caps.keyword_search is True
    assert caps.namespace_listing is True
    assert caps.vector_search is False


async def test_stored_content_is_valid_json(store: GCSMemoryStore) -> None:
    """The serialized payload round-trips as valid JSON."""
    await store.put(("u",), "k", {"v": 1, "nested": {"x": [1, 2, 3]}})
    item = await store.get(("u",), "k")
    assert item is not None
    assert item.value == {"v": 1, "nested": {"x": [1, 2, 3]}}
    blob = store._bucket().blob("memory/u/k.json")  # type: ignore[attr-defined]
    parsed: Any = json.loads(blob.download_as_text())
    assert parsed["v"] == 1
