"""MEM-C-08: :meth:`MemoryStore.as_langgraph_store` returns a working adapter.

We don't exercise LangGraph's private conformance suite (intentionally — the
surface is versioned differently from the harness) but we do round-trip the
five async methods the harness relies on: ``aput``, ``aget``, ``asearch``,
``adelete``, ``alist_namespaces``. The sync ``put``/``batch`` path is also
asserted to raise :class:`NotImplementedError` so consumers are nudged into
the async API (per spec).
"""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from src.core.harness.extensions.memory_stores import (  # noqa: E402
    InMemoryMemoryStore,
)

pytestmark = pytest.mark.asyncio


async def test_adapter_aput_aget_round_trip() -> None:
    """``aput`` followed by ``aget`` returns the stored value."""
    store = InMemoryMemoryStore()
    adapter = store.as_langgraph_store()
    await adapter.aput(("u",), "k", {"v": 1})
    item = await adapter.aget(("u",), "k")
    assert item is not None
    assert item.value == {"v": 1}
    assert item.namespace == ("u",)
    assert item.key == "k"


async def test_adapter_asearch_with_filter() -> None:
    """``asearch`` honors dict-subset filters via the MemoryStore."""
    store = InMemoryMemoryStore()
    adapter = store.as_langgraph_store()
    await adapter.aput(("u",), "a", {"kind": "note"})
    await adapter.aput(("u",), "b", {"kind": "task"})
    await adapter.aput(("u",), "c", {"kind": "note"})
    hits = await adapter.asearch(("u",), filter={"kind": "note"}, limit=10)
    assert {hit.key for hit in hits} == {"a", "c"}
    for hit in hits:
        assert hit.value["kind"] == "note"


async def test_adapter_adelete_then_aget_returns_none() -> None:
    """``adelete`` removes the item and subsequent ``aget`` yields ``None``."""
    store = InMemoryMemoryStore()
    adapter = store.as_langgraph_store()
    await adapter.aput(("u",), "k", {"v": 1})
    await adapter.adelete(("u",), "k")
    assert await adapter.aget(("u",), "k") is None


async def test_adapter_alist_namespaces_prefix_filter() -> None:
    """``alist_namespaces(prefix=...)`` returns matching namespaces."""
    store = InMemoryMemoryStore()
    adapter = store.as_langgraph_store()
    await adapter.aput(("a", "1"), "k", {"v": 1})
    await adapter.aput(("a", "2"), "k", {"v": 2})
    await adapter.aput(("b", "1"), "k", {"v": 3})
    scoped = await adapter.alist_namespaces(prefix=("a",))
    assert ("a", "1") in scoped
    assert ("a", "2") in scoped
    assert ("b", "1") not in scoped


async def test_adapter_sync_batch_raises() -> None:
    """Sync ``batch`` is unsupported and the adapter surfaces a clear error."""
    store = InMemoryMemoryStore()
    adapter = store.as_langgraph_store()
    with pytest.raises(NotImplementedError):
        adapter.batch([])
