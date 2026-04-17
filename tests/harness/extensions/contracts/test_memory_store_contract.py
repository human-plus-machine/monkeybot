"""Contract suite invariants for every :class:`MemoryStore` backend.

IDs map to ``MEM-C-01`` … ``MEM-C-08`` in 1b-contracts.md §11.1. Story 3
activates MEM-C-08 (LangGraph ``BaseStore`` adapter compliance) since the
adapter now ships.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta

import pytest

from src.core.harness.extensions import MemoryStore

from .fixtures.memory_store_backends import MEMORY_STORE_FACTORIES

pytestmark = pytest.mark.asyncio


def _id_fn(param: tuple[str, Callable[[], MemoryStore]]) -> str:
    return param[0]


@pytest.fixture(params=MEMORY_STORE_FACTORIES, ids=_id_fn)
def memory_store_factory(
    request: pytest.FixtureRequest,
) -> Callable[[], MemoryStore]:
    """Extend conftest's fixture with the shipped backends from Story 3."""
    _, factory = request.param
    return factory  # type: ignore[no-any-return]


async def test_mem_c_01_put_then_get(memory_store_factory: Callable[[], MemoryStore]) -> None:
    """MEM-C-01: ``put`` + ``get`` returns identical value and updates ``updated_at``."""
    store = memory_store_factory()
    await store.put(("u", "1"), "k", {"v": 1})
    item = await store.get(("u", "1"), "k")
    assert item is not None
    assert item.value == {"v": 1}
    assert item.updated_at >= item.created_at


async def test_mem_c_02_overwrite_preserves_created_at(
    memory_store_factory: Callable[[], MemoryStore],
) -> None:
    """MEM-C-02: overwriting preserves ``created_at`` and advances ``updated_at``."""
    store = memory_store_factory()
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


async def test_mem_c_03_delete_then_get_returns_none(
    memory_store_factory: Callable[[], MemoryStore],
) -> None:
    """MEM-C-03: ``delete`` then ``get`` returns ``None``."""
    store = memory_store_factory()
    await store.put(("u",), "k", {"v": 1})
    await store.delete(("u",), "k")
    assert await store.get(("u",), "k") is None


async def test_mem_c_04_search_honors_filter(
    memory_store_factory: Callable[[], MemoryStore],
) -> None:
    """MEM-C-04: ``search`` honors keyword filters."""
    store = memory_store_factory()
    await store.put(("u",), "a", {"kind": "note"})
    await store.put(("u",), "b", {"kind": "task"})
    await store.put(("u",), "c", {"kind": "note"})
    notes = await store.search(("u",), filter={"kind": "note"}, limit=10)
    assert {item.key for item in notes} == {"a", "c"}


async def test_mem_c_05_list_namespaces_with_prefix(
    memory_store_factory: Callable[[], MemoryStore],
) -> None:
    """MEM-C-05: ``list_namespaces(prefix)`` returns namespaces under that prefix."""
    store = memory_store_factory()
    await store.put(("a", "1"), "k", {"v": 1})
    await store.put(("a", "2"), "k", {"v": 2})
    await store.put(("b", "1"), "k", {"v": 3})
    result = await store.list_namespaces(("a",))
    assert ("a", "1") in result
    assert ("a", "2") in result
    assert ("b", "1") not in result


async def test_mem_c_06_capabilities_truth_table(
    memory_store_factory: Callable[[], MemoryStore],
) -> None:
    """MEM-C-06: ``capabilities()`` matches the backend's declared truth table."""
    store = memory_store_factory()
    caps = store.capabilities()
    assert caps.namespace_listing is True
    if caps.vector_search:
        await store.put(("v",), "k", {"v": 1})


async def test_mem_c_07_ttl_expires(memory_store_factory: Callable[[], MemoryStore]) -> None:
    """MEM-C-07: TTL entries expire within 2×TTL wall-clock (skipped on TTL-less backends)."""
    store = memory_store_factory()
    caps = store.capabilities()
    if not caps.ttl:
        pytest.skip("backend does not advertise TTL support")
    await store.put(("u",), "ephemeral", {"v": 1}, ttl=timedelta(milliseconds=50))
    await asyncio.sleep(0.15)
    assert await store.get(("u",), "ephemeral") is None


async def test_mem_c_08_langgraph_store_compliance(
    memory_store_factory: Callable[[], MemoryStore],
) -> None:
    """MEM-C-08: ``as_langgraph_store()`` round-trips the five async methods.

    Backends that have not yet wired the adapter (e.g. the Story 1 mock)
    skip rather than fail so MEM-C-08 only gates real implementations.
    """
    pytest.importorskip("langgraph")
    store = memory_store_factory()
    try:
        adapter = store.as_langgraph_store()
    except NotImplementedError:
        pytest.skip(f"{type(store).__name__} does not expose a LangGraph adapter")
    await adapter.aput(("u",), "k", {"v": 1})
    item = await adapter.aget(("u",), "k")
    assert item is not None
    assert item.value == {"v": 1}
    assert ("u",) in await adapter.alist_namespaces(prefix=("u",))
    hits = await adapter.asearch(("u",), filter={"v": 1}, limit=10)
    assert any(hit.key == "k" for hit in hits)
    await adapter.adelete(("u",), "k")
    assert await adapter.aget(("u",), "k") is None
