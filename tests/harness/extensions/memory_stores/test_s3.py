"""Integration tests for :class:`S3MemoryStore` using ``moto`` + ``aioboto3``.

Skipped cleanly when ``aioboto3`` or ``moto`` are missing. ``moto``'s
``mock_aws`` context manager intercepts boto3 traffic so the harness
exercises the real aioboto3 client without touching AWS.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("aioboto3")
pytest.importorskip("orjson")
pytest.importorskip("moto")

from moto import mock_aws  # noqa: E402

from src.core.harness.extensions._aws_clients import reset  # noqa: E402
from src.core.harness.extensions.memory_stores import S3MemoryStore  # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def store() -> AsyncIterator[S3MemoryStore]:
    bucket = f"test-memory-{uuid.uuid4().hex[:8]}"
    with mock_aws():
        import aioboto3

        session = aioboto3.Session(region_name="us-east-1")
        async with session.client("s3") as client:
            await client.create_bucket(Bucket=bucket)
        backend = S3MemoryStore(bucket=bucket, prefix="memory", region="us-east-1")
        try:
            yield backend
        finally:
            reset()


async def test_mem_c_01_put_then_get(store: S3MemoryStore) -> None:
    """MEM-C-01: put + get returns identical value."""
    await store.put(("u", "1"), "k", {"v": 1})
    item = await store.get(("u", "1"), "k")
    assert item is not None
    assert item.value == {"v": 1}
    assert item.namespace == ("u", "1")


async def test_mem_c_02_overwrite_preserves_created_at(store: S3MemoryStore) -> None:
    """MEM-C-02: overwriting preserves ``created_at`` in object metadata."""
    await store.put(("u",), "k", {"v": 1})
    first = await store.get(("u",), "k")
    assert first is not None
    await store.put(("u",), "k", {"v": 2})
    second = await store.get(("u",), "k")
    assert second is not None
    assert second.created_at == first.created_at
    assert second.value == {"v": 2}


async def test_mem_c_03_delete_then_get(store: S3MemoryStore) -> None:
    """MEM-C-03: ``delete`` followed by ``get`` returns ``None``."""
    await store.put(("u",), "k", {"v": 1})
    await store.delete(("u",), "k")
    assert await store.get(("u",), "k") is None


async def test_mem_c_04_search_honors_filter(store: S3MemoryStore) -> None:
    """MEM-C-04: ``search`` honors dict-subset filter."""
    await store.put(("u",), "a", {"kind": "note"})
    await store.put(("u",), "b", {"kind": "task"})
    await store.put(("u",), "c", {"kind": "note"})
    hits = await store.search(("u",), filter={"kind": "note"}, limit=10)
    assert {item.key for item in hits} == {"a", "c"}


async def test_mem_c_05_list_namespaces_prefix(store: S3MemoryStore) -> None:
    """MEM-C-05: ``list_namespaces`` honors prefix filter."""
    await store.put(("a", "1"), "k", {"v": 1})
    await store.put(("a", "2"), "k", {"v": 2})
    await store.put(("b", "1"), "k", {"v": 3})
    scoped = await store.list_namespaces(("a",))
    assert ("a", "1") in scoped
    assert ("a", "2") in scoped
    assert ("b", "1") not in scoped


async def test_mem_c_06_capabilities_no_keyword_search(store: S3MemoryStore) -> None:
    """MEM-C-06: S3 declares keyword_search=False (no efficient scan)."""
    caps = store.capabilities()
    assert caps.keyword_search is False
    assert caps.namespace_listing is True
    assert caps.vector_search is False
