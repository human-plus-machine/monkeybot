"""Unit tests for :class:`FirestoreMemoryStore` using an in-process fake.

The fake reproduces just enough of ``google.cloud.firestore.Client`` to
exercise the contract invariants (document-level ``get``/``set``/``delete``
+ collection-level ``where``/``stream``). The real SDK is an optional
dependency; these tests run without it installed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from src.core.harness.extensions.memory_stores import FirestoreMemoryStore

pytestmark = pytest.mark.asyncio


class _FakeSnapshot:
    def __init__(self, doc_id: str, data: dict[str, Any] | None) -> None:
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return None if self._data is None else dict(self._data)


class _FakeDocument:
    def __init__(self, store: dict[str, dict[str, Any]], doc_id: str) -> None:
        self._store = store
        self.id = doc_id

    def get(self) -> _FakeSnapshot:
        return _FakeSnapshot(self.id, self._store.get(self.id))

    def set(self, data: dict[str, Any]) -> None:
        self._store[self.id] = dict(data)

    def delete(self) -> None:
        self._store.pop(self.id, None)


class _FakeQuery:
    def __init__(
        self,
        store: dict[str, dict[str, Any]],
        filters: list[tuple[str, str, Any]] | None = None,
    ) -> None:
        self._store = store
        self._filters = list(filters or [])

    def where(self, field: str, op: str, value: Any) -> _FakeQuery:
        return _FakeQuery(self._store, [*self._filters, (field, op, value)])

    def stream(self) -> Iterable[_FakeSnapshot]:
        for doc_id, data in self._store.items():
            if all(
                op == "==" and data.get(field) == value
                for field, op, value in self._filters
            ):
                yield _FakeSnapshot(doc_id, data)


class _FakeCollection:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    def document(self, doc_id: str) -> _FakeDocument:
        return _FakeDocument(self._store, doc_id)

    def where(self, field: str, op: str, value: Any) -> _FakeQuery:
        return _FakeQuery(self._store).where(field, op, value)

    def stream(self) -> Iterable[_FakeSnapshot]:
        return _FakeQuery(self._store).stream()


class _FakeFirestoreClient:
    def __init__(self) -> None:
        self._collections: dict[str, dict[str, dict[str, Any]]] = {}

    def collection(self, name: str) -> _FakeCollection:
        store = self._collections.setdefault(name, {})
        return _FakeCollection(store)


@pytest.fixture
def store() -> FirestoreMemoryStore:
    backend = FirestoreMemoryStore(project_id="test", collection="memory")
    backend._client = _FakeFirestoreClient()  # type: ignore[attr-defined]
    return backend


async def test_mem_c_01_put_then_get(store: FirestoreMemoryStore) -> None:
    """MEM-C-01: put + get returns identical value."""
    await store.put(("u", "1"), "k", {"v": 1})
    item = await store.get(("u", "1"), "k")
    assert item is not None
    assert item.value == {"v": 1}
    assert item.namespace == ("u", "1")
    assert item.updated_at >= item.created_at


async def test_mem_c_02_overwrite_preserves_created_at(
    store: FirestoreMemoryStore,
) -> None:
    """MEM-C-02: overwriting preserves ``created_at``."""
    await store.put(("u",), "k", {"v": 1})
    first = await store.get(("u",), "k")
    assert first is not None
    await store.put(("u",), "k", {"v": 2})
    second = await store.get(("u",), "k")
    assert second is not None
    assert second.created_at == first.created_at
    assert second.value == {"v": 2}


async def test_mem_c_03_delete_then_get(store: FirestoreMemoryStore) -> None:
    """MEM-C-03: ``delete`` followed by ``get`` yields ``None``."""
    await store.put(("u",), "k", {"v": 1})
    await store.delete(("u",), "k")
    assert await store.get(("u",), "k") is None


async def test_mem_c_04_search_honors_filter(store: FirestoreMemoryStore) -> None:
    """MEM-C-04: ``search`` applies the dict-subset filter."""
    await store.put(("u",), "a", {"kind": "note"})
    await store.put(("u",), "b", {"kind": "task"})
    await store.put(("u",), "c", {"kind": "note"})
    hits = await store.search(("u",), filter={"kind": "note"}, limit=10)
    assert {item.key for item in hits} == {"a", "c"}


async def test_mem_c_05_list_namespaces_prefix(store: FirestoreMemoryStore) -> None:
    """MEM-C-05: ``list_namespaces`` honors the prefix filter."""
    await store.put(("a", "1"), "k", {"v": 1})
    await store.put(("a", "2"), "k", {"v": 2})
    await store.put(("b", "1"), "k", {"v": 3})
    scoped = await store.list_namespaces(("a",))
    assert ("a", "1") in scoped
    assert ("a", "2") in scoped
    assert ("b", "1") not in scoped


async def test_mem_c_06_capabilities(store: FirestoreMemoryStore) -> None:
    """MEM-C-06: declared capabilities match Firestore's behaviour (keyword yes, ttl no)."""
    caps = store.capabilities()
    assert caps.keyword_search is True
    assert caps.namespace_listing is True
    assert caps.ttl is False
    assert caps.vector_search is False
