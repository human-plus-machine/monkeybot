"""Testcontainers-gated integration tests for :class:`PostgresMemoryStore`.

Skipped cleanly when ``asyncpg``, ``orjson``, or ``testcontainers`` are
missing, or when Docker is not reachable. Also verifies that the
``enable_pgvector`` flag flips :meth:`capabilities` correctly without
requiring pgvector to be installed in the test container.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("asyncpg")
pytest.importorskip("orjson")
pytest.importorskip("testcontainers.postgres")

from src.core.harness.extensions._postgres_pool import close_all  # noqa: E402
from src.core.harness.extensions.memory_stores import PostgresMemoryStore  # noqa: E402

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="module")
def postgres_container() -> AsyncIterator[str]:  # type: ignore[misc]
    from testcontainers.postgres import PostgresContainer

    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:  # pragma: no cover - docker not reachable
        pytest.skip(f"Docker unavailable: {exc}")
        return  # type: ignore[unreachable]
    try:
        dsn = container.get_connection_url().replace("postgresql+psycopg2", "postgresql")
        yield dsn
    finally:
        container.stop()


@pytest.fixture
async def store(postgres_container: str) -> AsyncIterator[PostgresMemoryStore]:
    env_name = f"CKPT_DSN_{uuid.uuid4().hex[:8].upper()}"
    schema = f"mem_test_{uuid.uuid4().hex[:8]}"
    os.environ[env_name] = postgres_container
    backend = PostgresMemoryStore(dsn_env=env_name, schema_name=schema)
    try:
        yield backend
    finally:
        await close_all()
        os.environ.pop(env_name, None)


async def test_mem_c_01_put_then_get(store: PostgresMemoryStore) -> None:
    """MEM-C-01: put + get returns identical value."""
    await store.put(("u", "1"), "k", {"v": 1})
    item = await store.get(("u", "1"), "k")
    assert item is not None
    assert item.value == {"v": 1}
    assert item.namespace == ("u", "1")


async def test_mem_c_02_overwrite_preserves_created_at(
    store: PostgresMemoryStore,
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


async def test_mem_c_03_delete_then_get(store: PostgresMemoryStore) -> None:
    """MEM-C-03: delete + get yields ``None``."""
    await store.put(("u",), "k", {"v": 1})
    await store.delete(("u",), "k")
    assert await store.get(("u",), "k") is None


async def test_mem_c_04_search_honors_filter(store: PostgresMemoryStore) -> None:
    """MEM-C-04: ``value @> filter`` honors dict-subset filters."""
    await store.put(("u",), "a", {"kind": "note"})
    await store.put(("u",), "b", {"kind": "task"})
    await store.put(("u",), "c", {"kind": "note"})
    hits = await store.search(("u",), filter={"kind": "note"}, limit=10)
    assert {item.key for item in hits} == {"a", "c"}


async def test_mem_c_05_list_namespaces_prefix(store: PostgresMemoryStore) -> None:
    """MEM-C-05: ``list_namespaces`` honors prefix filter."""
    await store.put(("a", "1"), "k", {"v": 1})
    await store.put(("a", "2"), "k", {"v": 2})
    await store.put(("b", "1"), "k", {"v": 3})
    scoped = await store.list_namespaces(("a",))
    assert ("a", "1") in scoped
    assert ("a", "2") in scoped
    assert ("b", "1") not in scoped


async def test_mem_c_06_capabilities_default() -> None:
    """MEM-C-06: default ``enable_pgvector=False`` reports vector_search=False."""
    backend = PostgresMemoryStore(enable_pgvector=False)
    caps = backend.capabilities()
    assert caps.vector_search is False
    assert caps.keyword_search is True
    assert caps.ttl is True


async def test_mem_c_06_capabilities_pgvector() -> None:
    """MEM-C-06: ``enable_pgvector=True`` reports vector_search=True even without DB."""
    backend = PostgresMemoryStore(enable_pgvector=True)
    caps = backend.capabilities()
    assert caps.vector_search is True
