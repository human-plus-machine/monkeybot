"""Tests for knowledge SQLite FTS index."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from monkeybot.core.knowledge.chunking import chunk_text
from monkeybot.core.knowledge.extractors import content_hash
from monkeybot.core.knowledge.links import parse_wiki_links
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex, _pid_alive, _to_fts_query


def test_to_fts_query_uses_or_and_drops_stopwords() -> None:
    q = _to_fts_query(
        "How does getIdToken avoid races when multiple callers need a token "
        "while Firebase auth is still initializing?"
    )
    assert " OR " in q
    assert " AND " not in q
    assert "getIdToken" in q
    assert '"How"' not in q and '"does"' not in q


def test_to_fts_query_strips_embedded_quotes_defensively() -> None:
    """M1: even if a quote character reaches a token, the MATCH expr stays well-formed."""
    q = _to_fts_query('foo"bar baz"qux')
    assert q.count('"') % 2 == 0
    for part in q.split(" OR "):
        assert part.startswith('"') and part.endswith('"*')
        inner = part[1:-2]
        assert '"' not in inner


@pytest.mark.asyncio
async def test_open_warns_on_second_live_writer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """M3: opening the same index from a second live PID logs an advisory warning."""
    db = tmp_path / "index.sqlite"
    sentinel = db.with_suffix(db.suffix + ".writer-pid")
    other_pid = os.getpid() + 1
    # Best-effort: pick a PID that is very unlikely to collide with a real
    # process but treat as "alive" via a monkeypatch-free direct write.
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(str(other_pid), encoding="utf-8")

    index = KnowledgeIndex(db)
    with caplog.at_level(logging.WARNING, logger="monkeybot.core.knowledge.sqlite_index"):
        # Force the alive check to say "yes" regardless of real PID state,
        # since we cannot reliably fabricate a live foreign process in CI.
        import monkeybot.core.knowledge.sqlite_index as sqlite_index_mod

        original = sqlite_index_mod._pid_alive
        sqlite_index_mod._pid_alive = lambda _pid: True
        try:
            await index.open()
        finally:
            sqlite_index_mod._pid_alive = original
    try:
        assert any("already has an active writer" in r.message for r in caplog.records)
    finally:
        await index.close()


def test_pid_alive_false_for_nonexistent_pid() -> None:
    # A very large PID is virtually guaranteed not to exist.
    assert _pid_alive("999999999") is False


def test_pid_alive_true_for_current_process() -> None:
    assert _pid_alive(str(os.getpid())) is True


def test_pid_alive_false_for_garbage_input() -> None:
    assert _pid_alive("not-a-pid") is False
    assert _pid_alive("-1") is False


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
