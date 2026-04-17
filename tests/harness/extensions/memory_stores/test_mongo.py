"""Testcontainers-gated integration tests for :class:`MongoMemoryStore`.

Skipped cleanly when ``motor``, ``orjson``, or ``testcontainers`` are
missing, or when Docker is not reachable.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("motor")
pytest.importorskip("orjson")
pytest.importorskip("testcontainers.mongodb")

from src.core.harness.extensions._mongo_client import close_all  # noqa: E402
from src.core.harness.extensions.memory_stores import MongoMemoryStore  # noqa: E402

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="module")
def mongo_container() -> AsyncIterator[str]:  # type: ignore[misc]
    from testcontainers.mongodb import MongoDbContainer

    try:
        container = MongoDbContainer("mongo:7")
        container.start()
    except Exception as exc:  # pragma: no cover - docker not reachable
        pytest.skip(f"Docker unavailable: {exc}")
        return  # type: ignore[unreachable]
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture
async def store(mongo_container: str) -> AsyncIterator[MongoMemoryStore]:
    env_name = f"MONGO_URI_{uuid.uuid4().hex[:8].upper()}"
    database = f"emonk_test_{uuid.uuid4().hex[:8]}"
    os.environ[env_name] = mongo_container
    backend = MongoMemoryStore(uri_env=env_name, database=database)
    try:
        yield backend
    finally:
        await close_all()
        os.environ.pop(env_name, None)


async def test_mem_c_01_put_then_get(store: MongoMemoryStore) -> None:
    """MEM-C-01: put + get returns identical value."""
    await store.put(("u", "1"), "k", {"v": 1})
    item = await store.get(("u", "1"), "k")
    assert item is not None
    assert item.value == {"v": 1}
    assert item.namespace == ("u", "1")


async def test_mem_c_02_overwrite_preserves_created_at(
    store: MongoMemoryStore,
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


async def test_mem_c_03_delete_then_get(store: MongoMemoryStore) -> None:
    """MEM-C-03: delete + get returns None."""
    await store.put(("u",), "k", {"v": 1})
    await store.delete(("u",), "k")
    assert await store.get(("u",), "k") is None


async def test_mem_c_04_search_honors_filter(store: MongoMemoryStore) -> None:
    """MEM-C-04: search honors dict-subset filter."""
    await store.put(("u",), "a", {"kind": "note"})
    await store.put(("u",), "b", {"kind": "task"})
    await store.put(("u",), "c", {"kind": "note"})
    hits = await store.search(("u",), filter={"kind": "note"}, limit=10)
    assert {item.key for item in hits} == {"a", "c"}


async def test_mem_c_05_list_namespaces_prefix(store: MongoMemoryStore) -> None:
    """MEM-C-05: list_namespaces honors prefix filter."""
    await store.put(("a", "1"), "k", {"v": 1})
    await store.put(("a", "2"), "k", {"v": 2})
    await store.put(("b", "1"), "k", {"v": 3})
    scoped = await store.list_namespaces(("a",))
    assert ("a", "1") in scoped
    assert ("a", "2") in scoped
    assert ("b", "1") not in scoped


async def test_mem_c_06_capabilities() -> None:
    """MEM-C-06: declared Mongo capabilities match behaviour."""
    caps = MongoMemoryStore().capabilities()
    assert caps.keyword_search is True
    assert caps.namespace_listing is True
    assert caps.ttl is True
    assert caps.vector_search is False
