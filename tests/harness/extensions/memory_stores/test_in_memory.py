"""MEM-C-01 through MEM-C-07 invariants for :class:`InMemoryMemoryStore`.

The in-memory backend is the reference implementation for every memory-store
invariant (MEM-C-08 is exercised separately in :mod:`test_langgraph_adapter`).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from src.core.harness.extensions.memory_stores import InMemoryMemoryStore

pytestmark = pytest.mark.asyncio


async def test_mem_c_01_put_then_get() -> None:
    """MEM-C-01: put + get returns identical value and updated_at>=created_at."""
    store = InMemoryMemoryStore()
    await store.put(("u", "1"), "k", {"v": 1})
    item = await store.get(("u", "1"), "k")
    assert item is not None
    assert item.value == {"v": 1}
    assert item.namespace == ("u", "1")
    assert item.updated_at >= item.created_at


async def test_mem_c_02_overwrite_preserves_created_at() -> None:
    """MEM-C-02: overwriting preserves ``created_at``, advances ``updated_at``."""
    store = InMemoryMemoryStore()
    await store.put(("u",), "k", {"v": 1})
    first = await store.get(("u",), "k")
    assert first is not None
    await asyncio.sleep(0.01)
    await store.put(("u",), "k", {"v": 2})
    second = await store.get(("u",), "k")
    assert second is not None
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at
    assert second.value == {"v": 2}


async def test_mem_c_03_delete_then_get_returns_none() -> None:
    """MEM-C-03: delete + get returns ``None``."""
    store = InMemoryMemoryStore()
    await store.put(("u",), "k", {"v": 1})
    await store.delete(("u",), "k")
    assert await store.get(("u",), "k") is None


async def test_mem_c_04_search_honors_filter() -> None:
    """MEM-C-04: ``search`` honors dict-subset filters."""
    store = InMemoryMemoryStore()
    await store.put(("u",), "a", {"kind": "note"})
    await store.put(("u",), "b", {"kind": "task"})
    await store.put(("u",), "c", {"kind": "note"})
    notes = await store.search(("u",), filter={"kind": "note"}, limit=10)
    assert {item.key for item in notes} == {"a", "c"}


async def test_mem_c_04_search_honors_query() -> None:
    """MEM-C-04 extension: ``query`` keyword match against serialized value."""
    store = InMemoryMemoryStore()
    await store.put(("u",), "a", {"body": "hello world"})
    await store.put(("u",), "b", {"body": "totally unrelated"})
    hits = await store.search(("u",), query="hello", limit=10)
    assert {item.key for item in hits} == {"a"}


async def test_mem_c_05_list_namespaces_with_prefix() -> None:
    """MEM-C-05: ``list_namespaces(prefix)`` returns namespaces under that prefix."""
    store = InMemoryMemoryStore()
    await store.put(("a", "1"), "k", {"v": 1})
    await store.put(("a", "2"), "k", {"v": 2})
    await store.put(("b", "1"), "k", {"v": 3})
    result = await store.list_namespaces(("a",))
    assert ("a", "1") in result
    assert ("a", "2") in result
    assert ("b", "1") not in result


async def test_mem_c_05_list_namespaces_all() -> None:
    """MEM-C-05 extension: empty prefix returns every namespace."""
    store = InMemoryMemoryStore()
    await store.put(("a",), "k", {"v": 1})
    await store.put(("b",), "k", {"v": 1})
    all_ns = await store.list_namespaces()
    assert set(all_ns) == {("a",), ("b",)}


async def test_mem_c_06_capabilities_truth_table() -> None:
    """MEM-C-06: declared capabilities match the backend's behaviour."""
    caps = InMemoryMemoryStore().capabilities()
    assert caps.keyword_search is True
    assert caps.namespace_listing is True
    assert caps.ttl is True
    assert caps.vector_search is False
    assert caps.transactional is False


async def test_mem_c_07_ttl_expires() -> None:
    """MEM-C-07: TTL entries expire within 2×TTL wall-clock."""
    store = InMemoryMemoryStore()
    await store.put(("u",), "ephemeral", {"v": 1}, ttl=timedelta(milliseconds=50))
    await asyncio.sleep(0.15)
    assert await store.get(("u",), "ephemeral") is None


async def test_ttl_expires_excluded_from_list_and_search() -> None:
    """Additional coverage: expired entries disappear from search + namespace listing."""
    store = InMemoryMemoryStore()
    await store.put(("u",), "ephemeral", {"v": 1}, ttl=timedelta(milliseconds=20))
    await store.put(("u",), "persistent", {"v": 2})
    await asyncio.sleep(0.1)
    namespaces = await store.list_namespaces()
    assert namespaces == [("u",)]
    hits = await store.search(("u",), limit=10)
    assert {item.key for item in hits} == {"persistent"}
