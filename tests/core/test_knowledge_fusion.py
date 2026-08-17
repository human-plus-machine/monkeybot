"""Tests for knowledge fusion (FTS + graph + note bias)."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.knowledge.chunking import chunk_text
from monkeybot.core.knowledge.extractors import content_hash
from monkeybot.core.knowledge.fusion import search as recall
from monkeybot.core.knowledge.links import parse_wiki_links
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex


@pytest.mark.asyncio
async def test_graph_expand_from_note_to_workspace(tmp_path: Path) -> None:
    index = KnowledgeIndex(tmp_path / "i.sqlite")
    await index.open()
    try:
        note = (
            "# Refund policy\n\n"
            "Annual plans need approval.\n\n"
            "[[workspace:research/refunds.md#L1-5]]\n"
        )
        ws = "line1\nline2\nrefund policy for annual plans\nline4\nline5\n"
        await index.upsert_file(
            path="notes/refund-policy.md",
            source_type="note",
            content_hash=content_hash(note),
            mtime=1.0,
            chunks=chunk_text(note, path="notes/refund-policy.md", source_type="note"),
            links=parse_wiki_links(note, source_path="notes/refund-policy.md"),
        )
        await index.upsert_file(
            path="research/refunds.md",
            source_type="workspace_file",
            content_hash=content_hash(ws),
            mtime=1.0,
            chunks=chunk_text(
                ws, path="research/refunds.md", source_type="workspace_file"
            ),
        )

        hits = await recall(index, "annual plan approval", limit=10)
        paths = {h.path for h in hits}
        assert "notes/refund-policy.md" in paths
        assert "research/refunds.md" in paths
        note_hit = next(h for h in hits if h.path == "notes/refund-policy.md")
        assert any(ref.startswith("workspace:") for ref in note_hit.links)
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_recall_skips_chat_log_noise(tmp_path: Path) -> None:
    index = KnowledgeIndex(tmp_path / "i.sqlite")
    await index.open()
    try:
        code = "function getIdToken() { return authReadyPromise; }\n"
        chat = "How does getIdToken avoid races when multiple callers need a token?"
        await index.upsert_file(
            path="src/auth.ts",
            source_type="workspace_file",
            content_hash=content_hash(code),
            mtime=1.0,
            chunks=chunk_text(code, path="src/auth.ts", source_type="workspace_file"),
        )
        await index.upsert_file(
            path="memory/chat_log.md",
            source_type="note",
            content_hash=content_hash(chat),
            mtime=1.0,
            chunks=chunk_text(chat, path="memory/chat_log.md", source_type="note"),
        )
        hits = await recall(index, chat, limit=5)
        paths = [h.path for h in hits]
        assert "src/auth.ts" in paths
        assert "memory/chat_log.md" not in paths
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_collapse_duplicate_paths(tmp_path: Path) -> None:
    index = KnowledgeIndex(tmp_path / "i.sqlite")
    await index.open()
    try:
        # One file → multiple overlapping chunks that both FTS-match
        body = ("uniquezebra token appears here\n" * 40) + "uniquezebra again\n"
        await index.upsert_file(
            path="src/a.py",
            source_type="workspace_file",
            content_hash=content_hash(body),
            mtime=1.0,
            chunks=chunk_text(
                body,
                path="src/a.py",
                source_type="workspace_file",
                chunk_tokens=50,
                overlap_ratio=0.4,
            ),
        )
        hits = await recall(index, "uniquezebra", limit=10)
        paths = [h.path for h in hits]
        assert paths.count("src/a.py") == 1
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_demotes_lockfile_hits(tmp_path: Path) -> None:
    index = KnowledgeIndex(tmp_path / "i.sqlite")
    await index.open()
    try:
        code = "export function proxy() { return redirect www to apex }\n"
        lock = "lockfile css-calc redirect www apex middleware packages\n" * 5
        await index.upsert_file(
            path="src/proxy.ts",
            source_type="workspace_file",
            content_hash=content_hash(code),
            mtime=1.0,
            chunks=chunk_text(code, path="src/proxy.ts", source_type="workspace_file"),
        )
        await index.upsert_file(
            path="pnpm-lock.yaml",
            source_type="workspace_file",
            content_hash=content_hash(lock),
            mtime=1.0,
            chunks=chunk_text(
                lock, path="pnpm-lock.yaml", source_type="workspace_file"
            ),
        )
        hits = await recall(index, "www apex redirect middleware", limit=5)
        assert hits[0].path == "src/proxy.ts"
        lock_hits = [h for h in hits if h.path.endswith("pnpm-lock.yaml")]
        if lock_hits:
            assert lock_hits[0].score < hits[0].score
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_graph_only_hits_use_rrf_not_absolute_score(tmp_path: Path) -> None:
    """Seq-173 regression: graph fan-out must not swamp organic FTS hits.

    A hub note with many links used to inject graph-only targets at
    ``_GRAPH_BASE_SCORE=0.35`` (~12x any RRF score). Those targets now
    participate in RRF as a third list and stay below dual-signal hits.
    """
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

    index = KnowledgeIndex(tmp_path / "i.sqlite")
    vectors = SQLiteVectorStore(tmp_path / "v.sqlite")
    await index.open()
    await vectors.open()
    try:
        query = "NEXT_PUBLIC_SITE_URL production App Hosting config"
        # Organic evidence: matches FTS *and* ANN
        evidence = (
            "NEXT_PUBLIC_SITE_URL is set in apphosting.yaml for production "
            "App Hosting deploys.\n"
        )
        await index.upsert_file(
            path="apphosting.yaml",
            source_type="workspace_file",
            content_hash=content_hash(evidence),
            mtime=1.0,
            chunks=chunk_text(
                evidence, path="apphosting.yaml", source_type="workspace_file"
            ),
        )
        await vectors.upsert(
            [
                VectorChunkRecord(
                    chunk_id="apphosting.yaml#L1-1",
                    path="apphosting.yaml",
                    vector=[1.0, 0.0],
                    model_id="fake",
                    dim=2,
                    start_line=1,
                    end_line=1,
                    source_type="workspace_file",
                )
            ]
        )

        # Hub note that FTS-matches and fans out to N linked notes (no kw/vec match)
        n_links = 10
        link_lines = "\n".join(
            f"[[linked/noise-{i:02d}.md]]" for i in range(n_links)
        )
        hub = (
            f"# Index\n\nMentions {query} briefly.\n\n"
            f"{link_lines}\n"
        )
        await index.upsert_file(
            path="notes/hub.md",
            source_type="note",
            content_hash=content_hash(hub),
            mtime=1.0,
            chunks=chunk_text(hub, path="notes/hub.md", source_type="note"),
            links=parse_wiki_links(hub, source_path="notes/hub.md"),
        )
        for i in range(n_links):
            noise = f"# tool echo {i}\nglob returned zero results in 25ms\n"
            path = f"notes/linked/noise-{i:02d}.md"
            await index.upsert_file(
                path=path,
                source_type="note",
                content_hash=content_hash(noise),
                mtime=1.0,
                chunks=chunk_text(noise, path=path, source_type="note"),
            )

        hits = await recall(
            index,
            query,
            limit=10,
            embedding_provider=_FakeEmbedder(),
            vector_store=vectors,
        )
        assert hits
        assert hits[0].path == "apphosting.yaml"

        # Dual-signal organic hit must outrank graph-only noise (post-normalize)
        graph_only = [
            h
            for h in hits
            if h.path.startswith("notes/linked/")
            and h.via
            and str(h.via).startswith("graph:")
        ]
        assert hits[0].score == pytest.approx(1.0)
        for h in graph_only:
            assert h.score < hits[0].score
        # Even if noise fills remaining slots, organic evidence stays first
        assert all(h.path != "apphosting.yaml" or i == 0 for i, h in enumerate(hits))
    finally:
        await index.close()
        await vectors.close()


@pytest.mark.asyncio
async def test_score_normalized_and_signals_exposed(tmp_path: Path) -> None:
    """F4: top hit score == 1.0; bm25/cosine/signals populated when available."""
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

    index = KnowledgeIndex(tmp_path / "i.sqlite")
    vectors = SQLiteVectorStore(tmp_path / "v.sqlite")
    await index.open()
    await vectors.open()
    try:
        body = "authReadyPromise resolves once for getIdToken callers\n"
        other = "unrelated widget rendering helper\n"
        await index.upsert_file(
            path="src/auth.ts",
            source_type="workspace_file",
            content_hash=content_hash(body),
            mtime=1.0,
            chunks=chunk_text(body, path="src/auth.ts", source_type="workspace_file"),
        )
        await index.upsert_file(
            path="src/other.ts",
            source_type="workspace_file",
            content_hash=content_hash(other),
            mtime=1.0,
            chunks=chunk_text(other, path="src/other.ts", source_type="workspace_file"),
        )
        await vectors.upsert(
            [
                VectorChunkRecord(
                    chunk_id="src/auth.ts#L1-1",
                    path="src/auth.ts",
                    vector=[1.0, 0.0],
                    model_id="fake",
                    dim=2,
                    start_line=1,
                    end_line=1,
                    source_type="workspace_file",
                )
            ]
        )
        hits = await recall(
            index,
            "getIdToken authReadyPromise",
            limit=5,
            embedding_provider=_FakeEmbedder(),
            vector_store=vectors,
        )
        assert hits
        assert hits[0].score == pytest.approx(1.0)
        assert all(0.0 < h.score <= 1.0 + 1e-9 for h in hits)
        auth = next(h for h in hits if h.path == "src/auth.ts")
        assert auth.bm25 is not None
        assert auth.cosine is not None
        assert "fts" in auth.signals
        assert "ann" in auth.signals
        payload = auth.to_dict()
        assert payload["score"] == pytest.approx(1.0, abs=1e-3)
        assert "cosine" in payload
        assert "bm25" in payload
        assert "signals" in payload
    finally:
        await index.close()
        await vectors.close()


@pytest.mark.asyncio
async def test_path_stem_boost_promotes_filename_match(tmp_path: Path) -> None:
    """F5: query token matching basename stem lifts the evidence file into top 3."""
    index = KnowledgeIndex(tmp_path / "i.sqlite")
    await index.open()
    try:
        # Neighbor files share lexical tokens but lack the stem match
        evidence = "export const firebaseConfig = { apiKey: 'x' }\n"
        neighbor_a = "firebase hosting rewrite rules documented here apiKey\n"
        neighbor_b = "apiKey used by client SDK initialization helpers\n"
        await index.upsert_file(
            path="lib/firebase.ts",
            source_type="workspace_file",
            content_hash=content_hash(evidence),
            mtime=1.0,
            chunks=chunk_text(
                evidence, path="lib/firebase.ts", source_type="workspace_file"
            ),
        )
        await index.upsert_file(
            path="docs/hosting.md",
            source_type="workspace_file",
            content_hash=content_hash(neighbor_a),
            mtime=1.0,
            chunks=chunk_text(
                neighbor_a, path="docs/hosting.md", source_type="workspace_file"
            ),
        )
        await index.upsert_file(
            path="lib/client.ts",
            source_type="workspace_file",
            content_hash=content_hash(neighbor_b),
            mtime=1.0,
            chunks=chunk_text(
                neighbor_b, path="lib/client.ts", source_type="workspace_file"
            ),
        )
        hits = await recall(index, "where is the firebase config apiKey", limit=5)
        paths = [h.path for h in hits]
        assert "lib/firebase.ts" in paths[:3]
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_rrf_k_affects_rank_separation(tmp_path: Path) -> None:
    """F6: smaller K increases separation between rank-1 and rank-4 raw contributions."""
    index = KnowledgeIndex(tmp_path / "i.sqlite")
    await index.open()
    try:
        for i, name in enumerate(["a", "b", "c", "d"]):
            body = f"sharedtoken appears in file {name} uniquely\n" * 3
            path = f"src/{name}.ts"
            await index.upsert_file(
                path=path,
                source_type="workspace_file",
                content_hash=content_hash(body),
                mtime=1.0,
                chunks=chunk_text(body, path=path, source_type="workspace_file"),
            )
        hits_wide = await recall(index, "sharedtoken", limit=4, rrf_k=60)
        hits_sharp = await recall(index, "sharedtoken", limit=4, rrf_k=20)
        assert hits_wide and hits_sharp
        # Normalized scores: ratio of #1 to last should be larger with sharper K
        # when there is real rank separation (same set of single-signal hits).
        assert hits_wide[0].score == pytest.approx(1.0)
        assert hits_sharp[0].score == pytest.approx(1.0)
        if len(hits_wide) >= 4 and len(hits_sharp) >= 4:
            gap_wide = hits_wide[0].score - hits_wide[3].score
            gap_sharp = hits_sharp[0].score - hits_sharp[3].score
            assert gap_sharp >= gap_wide - 1e-9
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_relative_note_link_resolves_for_graph_snippet(tmp_path: Path) -> None:
    """F2: relative [[sibling/x]] from a note resolves and yields a graph snippet."""
    index = KnowledgeIndex(tmp_path / "i.sqlite")
    await index.open()
    try:
        hub = "# Hub\n\nuniquehubtoken links out.\n\n[[sibling/policy.md]]\n"
        target = "# Policy\n\nAnnual details live only in the linked note.\n"
        await index.upsert_file(
            path="notes/hub.md",
            source_type="note",
            content_hash=content_hash(hub),
            mtime=1.0,
            chunks=chunk_text(hub, path="notes/hub.md", source_type="note"),
            links=parse_wiki_links(hub, source_path="notes/hub.md"),
        )
        await index.upsert_file(
            path="notes/sibling/policy.md",
            source_type="note",
            content_hash=content_hash(target),
            mtime=1.0,
            chunks=chunk_text(
                target, path="notes/sibling/policy.md", source_type="note"
            ),
        )
        links = parse_wiki_links(hub, source_path="notes/hub.md")
        assert links[0].target_path == "notes/sibling/policy.md"

        hits = await recall(index, "uniquehubtoken", limit=10)
        policy = next(h for h in hits if h.path == "notes/sibling/policy.md")
        assert policy.snippet
        assert "Annual details" in (policy.snippet or "")
        assert policy.via and str(policy.via).startswith("graph:")
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_note_bias_on_equal_score(tmp_path: Path) -> None:
    index = KnowledgeIndex(tmp_path / "i.sqlite")
    await index.open()
    try:
        shared = "zebraunique token appears here once"
        await index.upsert_file(
            path="notes/a.md",
            source_type="note",
            content_hash=content_hash(shared + " note"),
            mtime=1.0,
            chunks=chunk_text(
                shared + " note", path="notes/a.md", source_type="note"
            ),
        )
        await index.upsert_file(
            path="code/b.py",
            source_type="workspace_file",
            content_hash=content_hash(shared + " code"),
            mtime=1.0,
            chunks=chunk_text(
                shared + " code", path="code/b.py", source_type="workspace_file"
            ),
        )
        hits = await recall(index, "zebraunique", limit=5)
        assert hits
        note_idx = next(i for i, h in enumerate(hits) if h.source_type == "note")
        ws_idx = next(i for i, h in enumerate(hits) if h.source_type == "workspace_file")
        assert note_idx < ws_idx
    finally:
        await index.close()
