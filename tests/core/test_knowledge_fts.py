"""Tests for knowledge SQLite FTS index."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.knowledge.chunking import chunk_text
from monkeybot.core.knowledge.extractors import content_hash
from monkeybot.core.knowledge.links import parse_wiki_links
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex, _to_fts_query


def test_to_fts_query_uses_or_and_drops_stopwords() -> None:
    q = _to_fts_query(
        "How does getIdToken avoid races when multiple callers need a token "
        "while Firebase auth is still initializing?"
    )
    assert " OR " in q
    assert " AND " not in q
    assert "getIdToken" in q
    assert '"How"' not in q and '"does"' not in q


@pytest.mark.asyncio
async def test_fts_upsert_and_search(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    index = KnowledgeIndex(db)
    await index.open()
    try:
        text = "Annual-plan refunds require manager approval within 14 days."
        chunks = chunk_text(text, path="notes/refund.md", source_type="note")
        await index.upsert_file(
            path="notes/refund.md",
            source_type="note",
            content_hash=content_hash(text),
            mtime=1.0,
            chunks=chunks,
            links=parse_wiki_links("[[workspace:research/refunds.md#L10-20]]"),
        )
        hits = await index.fts_search("refunds approval", limit=5)
        assert hits
        assert hits[0]["path"] == "notes/refund.md"
        assert "refund" in hits[0]["text"].lower()

        links = await index.links_from("notes/refund.md")
        assert len(links) == 1
        assert links[0]["target_path"] == "research/refunds.md"
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_fts_natural_language_or_matches(tmp_path: Path) -> None:
    """NL questions should still hit code that only shares some keywords."""
    db = tmp_path / "index.sqlite"
    index = KnowledgeIndex(db)
    await index.open()
    try:
        text = (
            "export async function getIdToken() {\n"
            "  if (!authReadyPromise) {\n"
            "    authReadyPromise = new Promise((resolve) => {\n"
            "      onAuthStateChanged(auth, () => resolve());\n"
            "    });\n"
            "  }\n"
            "  await authReadyPromise;\n"
            "}\n"
        )
        await index.upsert_file(
            path="auriga-web/src/lib/api/contextengine.ts",
            source_type="workspace_file",
            content_hash=content_hash(text),
            mtime=1.0,
            chunks=chunk_text(
                text,
                path="auriga-web/src/lib/api/contextengine.ts",
                source_type="workspace_file",
            ),
        )
        chat = (
            "How does getIdToken avoid races when multiple callers need a token "
            "while Firebase auth is still initializing?"
        )
        await index.upsert_file(
            path="memory/chat_log.md",
            source_type="note",
            content_hash=content_hash(chat),
            mtime=1.0,
            chunks=chunk_text(chat, path="memory/chat_log.md", source_type="note"),
        )

        hits = await index.fts_search(chat, limit=10)
        paths = [h["path"] for h in hits]
        assert "auriga-web/src/lib/api/contextengine.ts" in paths
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_hash_skip_and_delete(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    index = KnowledgeIndex(db)
    await index.open()
    try:
        text = "hello world uniquephrase"
        chunks = chunk_text(text, path="a.md", source_type="workspace_file")
        digest = content_hash(text)
        await index.upsert_file(
            path="a.md",
            source_type="workspace_file",
            content_hash=digest,
            mtime=1.0,
            chunks=chunks,
        )
        assert await index.get_file_hash("a.md") == digest
        await index.delete_path("a.md")
        assert await index.get_file_hash("a.md") is None
        assert await index.fts_search("uniquephrase") == []
    finally:
        await index.close()
