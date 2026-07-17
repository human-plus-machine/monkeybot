"""Tests for SQLite vector store (brute-force cosine)."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.persistence.sqlite_vector import SQLiteVectorStore
from monkeybot.core.persistence.vector_backends import VectorChunkRecord, create_vector_store


@pytest.mark.asyncio
async def test_sqlite_vector_upsert_query_delete(tmp_path: Path) -> None:
    store = SQLiteVectorStore(tmp_path / "v.sqlite")
    await store.open()
    try:
        await store.upsert(
            [
                VectorChunkRecord(
                    chunk_id="a.py#L1-2",
                    path="a.py",
                    vector=[1.0, 0.0, 0.0],
                    model_id="test",
                    dim=3,
                    start_line=1,
                    end_line=2,
                    source_type="workspace_file",
                ),
                VectorChunkRecord(
                    chunk_id="b.py#L1-2",
                    path="b.py",
                    vector=[0.0, 1.0, 0.0],
                    model_id="test",
                    dim=3,
                    start_line=1,
                    end_line=2,
                    source_type="workspace_file",
                ),
            ]
        )
        hits = await store.query([1.0, 0.0, 0.0], limit=2)
        assert hits[0].path == "a.py"
        assert hits[0].score > hits[1].score

        await store.delete_by_path("a.py")
        hits2 = await store.query([1.0, 0.0, 0.0], limit=5)
        assert all(h.path != "a.py" for h in hits2)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_vector_delete_missing(tmp_path: Path) -> None:
    store = SQLiteVectorStore(tmp_path / "v.sqlite")
    await store.open()
    try:
        await store.upsert(
            [
                VectorChunkRecord(
                    chunk_id="keep.py#L1-1",
                    path="keep.py",
                    vector=[1.0, 0.0],
                    model_id="t",
                    dim=2,
                    start_line=1,
                    end_line=1,
                ),
                VectorChunkRecord(
                    chunk_id="gone.py#L1-1",
                    path="gone.py",
                    vector=[0.0, 1.0],
                    model_id="t",
                    dim=2,
                    start_line=1,
                    end_line=1,
                ),
            ]
        )
        n = await store.delete_missing({"keep.py"})
        assert n == 1
        hits = await store.query([0.0, 1.0], limit=5)
        assert all(h.path == "keep.py" for h in hits)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_vector_path_collapse_and_matryoshka(tmp_path: Path) -> None:
    store = SQLiteVectorStore(tmp_path / "v.sqlite")
    await store.open()
    try:
        # Two chunks same path; query prefers the better one after collapse
        await store.upsert(
            [
                VectorChunkRecord(
                    chunk_id="a.py#L1-2",
                    path="a.py",
                    vector=[1.0, 0.0, 0.0, 0.0],
                    model_id="t",
                    dim=4,
                    start_line=1,
                    end_line=2,
                ),
                VectorChunkRecord(
                    chunk_id="a.py#L3-4",
                    path="a.py",
                    vector=[0.2, 0.8, 0.0, 0.0],
                    model_id="t",
                    dim=4,
                    start_line=3,
                    end_line=4,
                ),
                VectorChunkRecord(
                    chunk_id="pnpm-lock.yaml#L1-1",
                    path="pnpm-lock.yaml",
                    vector=[1.0, 0.0, 0.0, 0.0],
                    model_id="t",
                    dim=4,
                    start_line=1,
                    end_line=1,
                ),
            ]
        )
        hits = await store.query([1.0, 0.0, 0.0, 0.0], limit=5, dimensions=2)
        assert all(h.path != "pnpm-lock.yaml" for h in hits)
        assert sum(1 for h in hits if h.path == "a.py") == 1
        assert hits[0].chunk_id == "a.py#L1-2"
    finally:
        await store.close()


def test_create_vector_store_sqlite(tmp_path: Path) -> None:
    store = create_vector_store({"type": "sqlite", "path": str(tmp_path / "x.sqlite")})
    assert store is not None
