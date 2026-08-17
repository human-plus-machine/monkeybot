"""Tests for knowledge fusion with optional ANN stage."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.knowledge.chunking import chunk_text
from monkeybot.core.knowledge.extractors import content_hash
from monkeybot.core.knowledge.fusion import search as recall
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex
from monkeybot.core.persistence.sqlite_vector import SQLiteVectorStore
from monkeybot.core.persistence.sqlite_vector import VectorChunkRecord


class _FakeEmbedder:
    model_id = "fake"
    dim = 2

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        del text
        return [1.0, 0.0]


@pytest.mark.asyncio
async def test_ann_surfaces_paraphrase_neighbor(tmp_path: Path) -> None:
    """ANN can rank a file that FTS misses on a paraphrase query."""
    index = KnowledgeIndex(tmp_path / "i.sqlite")
    vectors = SQLiteVectorStore(tmp_path / "v.sqlite")
    await index.open()
    await vectors.open()
    try:
        # Lexical: "react-aria" — paraphrase query won't FTS-match well with OR stopwords alone
        code = (
            "import { Button } from 'react-aria-components'\n"
            "export function PrimaryButton() { return <Button /> }\n"
        )
        other = "export const unused = 1\n"
        await index.upsert_file(
            path="components/ui/button.tsx",
            source_type="workspace_file",
            content_hash=content_hash(code),
            mtime=1.0,
            chunks=chunk_text(
                code, path="components/ui/button.tsx", source_type="workspace_file"
            ),
        )
        await index.upsert_file(
            path="lib/unused.ts",
            source_type="workspace_file",
            content_hash=content_hash(other),
            mtime=1.0,
            chunks=chunk_text(
                other, path="lib/unused.ts", source_type="workspace_file"
            ),
        )

        # Only the button file is in the vector store, aligned with query embedding
        await vectors.upsert(
            [
                VectorChunkRecord(
                    chunk_id="components/ui/button.tsx#L1-2",
                    path="components/ui/button.tsx",
                    vector=[1.0, 0.0],
                    model_id="fake",
                    dim=2,
                    start_line=1,
                    end_line=2,
                    source_type="workspace_file",
                )
            ]
        )

        # Query avoids the identifier "react-aria" — FTS alone is weak; ANN should help
        hits = await recall(
            index,
            "which library powers the shared Button primitive",
            limit=5,
            embedding_provider=_FakeEmbedder(),
            vector_store=vectors,
        )
        paths = [h.path for h in hits]
        assert "components/ui/button.tsx" in paths
        button = next(h for h in hits if h.path == "components/ui/button.tsx")
        assert button.via == "ann" or button.score > 0
    finally:
        await index.close()
        await vectors.close()


@pytest.mark.asyncio
async def test_ann_drops_orphan_vector_without_fts_chunk(tmp_path: Path) -> None:
    """Stale ANN rows whose FTS chunk is gone must not surface as hits.

    Simulates the crash window between FTS delete and vector prune: a vector
    hit resolves to no chunk snippet and must be soft-dropped by ``_ingest_ann``.
    """
    index = KnowledgeIndex(tmp_path / "i.sqlite")
    vectors = SQLiteVectorStore(tmp_path / "v.sqlite")
    await index.open()
    await vectors.open()
    try:
        keep = "keep alive token UNIQUE_KEEP_ZX\n"
        await index.upsert_file(
            path="keep.py",
            source_type="workspace_file",
            content_hash=content_hash(keep),
            mtime=1.0,
            chunks=chunk_text(keep, path="keep.py", source_type="workspace_file"),
        )
        # Orphan vector only — no FTS row for gone.py
        await vectors.upsert(
            [
                VectorChunkRecord(
                    chunk_id="gone.py#L1-1",
                    path="gone.py",
                    vector=[1.0, 0.0],
                    model_id="fake",
                    dim=2,
                    start_line=1,
                    end_line=1,
                    source_type="workspace_file",
                    text="orphan snippet that must not surface",
                ),
                VectorChunkRecord(
                    chunk_id="keep.py#L1-1",
                    path="keep.py",
                    vector=[0.0, 1.0],
                    model_id="fake",
                    dim=2,
                    start_line=1,
                    end_line=1,
                    source_type="workspace_file",
                ),
            ]
        )

        class _OrphanPreferringEmbedder:
            model_id = "fake"
            dim = 2

            async def embed_documents(self, texts: list[str]) -> list[list[float]]:
                return [[0.0, 1.0] for _ in texts]

            async def embed_query(self, text: str) -> list[float]:
                del text
                # Prefer the orphan vector over keep.py
                return [1.0, 0.0]

        hits = await recall(
            index,
            "totally unrelated paraphrase that misses FTS",
            limit=10,
            embedding_provider=_OrphanPreferringEmbedder(),
            vector_store=vectors,
        )
        paths = [h.path for h in hits]
        assert "gone.py" not in paths
        assert all("orphan snippet" not in (h.snippet or "") for h in hits)
    finally:
        await index.close()
        await vectors.close()
