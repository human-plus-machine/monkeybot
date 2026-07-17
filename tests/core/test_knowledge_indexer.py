"""Tests for KnowledgeIndexer scan / hash skip / ignore dirs."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.knowledge.indexer import KnowledgeIndexer
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex
from monkeybot.core.knowledge.types import KnowledgeSettings


@pytest.mark.asyncio
async def test_indexer_startup_scan_and_hash_skip(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "hello.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "pkg.js").write_text("ignored", encoding="utf-8")

    knowledge = tmp_path / ".monkeybot" / "knowledge"
    notes = knowledge / "notes"
    notes.mkdir(parents=True)
    (notes / "tip.md").write_text(
        "Use hello.\n\n[[workspace:hello.py#L1-2]]\n",
        encoding="utf-8",
    )

    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge),
        index_path=str(knowledge / "index.sqlite"),
        debounce_ms=0,
        startup_scan=True,
        chunk_tokens=200,
    )
    index = KnowledgeIndex(Path(settings.index_path))
    await index.open()
    try:
        indexer = KnowledgeIndexer(
            index,
            workspace_root=ws,
            knowledge_root=knowledge,
            settings=settings,
        )
        await indexer.ensure_ready()
        paths = await index.list_paths()
        assert "hello.py" in paths
        assert "notes/tip.md" in paths
        assert not any("node_modules" in p for p in paths)

        # Hash skip: second scan should not error; hash unchanged
        h1 = await index.get_file_hash("hello.py")
        await indexer._full_scan()  # noqa: SLF001 — intentional
        assert await index.get_file_hash("hello.py") == h1

        # Delete missing
        (ws / "hello.py").unlink()
        await indexer._full_scan()  # noqa: SLF001
        assert "hello.py" not in await index.list_paths()
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_workspace_rescan_after_shell_clone(tmp_path: Path) -> None:
    """Simulates git clone: files appear without write_file; rescan picks them up."""
    from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
    from monkeybot.core.knowledge.hook import KnowledgeHook

    ws = tmp_path / "workspace"
    ws.mkdir()
    knowledge = tmp_path / ".monkeybot" / "knowledge"
    knowledge.mkdir(parents=True)

    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge),
        index_path=str(knowledge / "index.sqlite"),
        debounce_ms=0,
        startup_scan=True,
        chunk_tokens=200,
    )
    index = KnowledgeIndex(Path(settings.index_path))
    await index.open()
    try:
        indexer = KnowledgeIndexer(
            index,
            workspace_root=ws,
            knowledge_root=knowledge,
            settings=settings,
        )
        await indexer.ensure_ready()
        assert "cloned/app.py" not in await index.list_paths()

        # Agent clones via run_command — files land on disk outside write tools
        clone_dir = ws / "cloned"
        clone_dir.mkdir()
        (clone_dir / "app.py").write_text(
            "def main():\n    return 'cloned'\n", encoding="utf-8"
        )

        hook = KnowledgeHook(indexer)
        mgr = HookManager()
        hook.register(mgr)

        class _Ctx:
            pass

        await mgr.fire(
            HookPayload(
                event=HookEvent.POST_TOOL,
                thread_id="t",
                request_id="r",
                ctx=_Ctx(),  # type: ignore[arg-type]
                tool_name="run_command",
                tool_args={"argv": ["git", "clone", "…"]},
                tool_result='{"ok": true, "exit_code": 0}',
            )
        )
        await indexer.flush()

        paths = await index.list_paths()
        assert "cloned/app.py" in paths
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_failed_run_command_does_not_rescan(tmp_path: Path) -> None:
    from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
    from monkeybot.core.knowledge.hook import KnowledgeHook

    ws = tmp_path / "workspace"
    ws.mkdir()
    knowledge = tmp_path / ".monkeybot" / "knowledge"
    knowledge.mkdir(parents=True)
    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge),
        index_path=str(knowledge / "index.sqlite"),
        debounce_ms=0,
        startup_scan=False,
    )
    index = KnowledgeIndex(Path(settings.index_path))
    await index.open()
    try:
        indexer = KnowledgeIndexer(
            index,
            workspace_root=ws,
            knowledge_root=knowledge,
            settings=settings,
        )
        await indexer.ensure_ready()
        (ws / "new.py").write_text("x = 1\n", encoding="utf-8")

        hook = KnowledgeHook(indexer)
        mgr = HookManager()
        hook.register(mgr)

        class _Ctx:
            pass

        await mgr.fire(
            HookPayload(
                event=HookEvent.POST_TOOL,
                thread_id="t",
                request_id="r",
                ctx=_Ctx(),  # type: ignore[arg-type]
                tool_name="run_command",
                tool_args={"argv": ["false"]},
                tool_result='{"ok": false, "exit_code": 1}',
            )
        )
        await indexer.flush()
        assert "new.py" not in await index.list_paths()
    finally:
        await index.close()


class _FakeEmbedder:
    model_id = "fake"
    dim = 2

    def __init__(self) -> None:
        self.calls = 0

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0, 0.0] for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        del text
        return [1.0, 0.0]


@pytest.mark.asyncio
async def test_indexer_backfills_embeddings_on_hash_skip(tmp_path: Path) -> None:
    """When FTS is current but vectors empty, startup scan still embeds."""
    from monkeybot.core.persistence.sqlite_vector import SQLiteVectorStore

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "hello.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    knowledge = tmp_path / ".monkeybot" / "knowledge"
    knowledge.mkdir(parents=True)

    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge),
        index_path=str(knowledge / "index.sqlite"),
        debounce_ms=0,
        startup_scan=True,
        chunk_tokens=200,
    )
    index = KnowledgeIndex(Path(settings.index_path))
    await index.open()
    vectors = SQLiteVectorStore(knowledge / "vectors.sqlite")
    await vectors.open()
    embedder = _FakeEmbedder()
    try:
        # Phase 1 style: index without embeddings
        indexer = KnowledgeIndexer(
            index,
            workspace_root=ws,
            knowledge_root=knowledge,
            settings=settings,
        )
        await indexer.ensure_ready()
        assert await index.get_file_hash("hello.py")

        # Enable embeddings on the same FTS index
        indexer2 = KnowledgeIndexer(
            index,
            workspace_root=ws,
            knowledge_root=knowledge,
            settings=settings,
            embedding_provider=embedder,
            vector_store=vectors,
        )
        await indexer2._full_scan()  # noqa: SLF001
        assert embedder.calls >= 1
        assert await vectors.has_path("hello.py")
        hits = await vectors.query([1.0, 0.0], limit=5)
        assert any(h.path == "hello.py" for h in hits)
    finally:
        await index.close()
        await vectors.close()


@pytest.mark.asyncio
async def test_indexer_skips_episodic_and_semantic_memory(tmp_path: Path) -> None:
    """F3: auto-capture episodic/semantic notes are never indexed."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    knowledge = tmp_path / ".monkeybot" / "knowledge"
    knowledge.mkdir(parents=True)
    memory = tmp_path / "data" / "memory"
    (memory / "episodic").mkdir(parents=True)
    (memory / "semantic").mkdir(parents=True)
    (memory / "INDEX.md").write_text("# Memory index\n", encoding="utf-8")
    (memory / "episodic" / "tool.md").write_text(
        "glob returned zero results in 25ms\n", encoding="utf-8"
    )
    (memory / "semantic" / "echo.md").write_text(
        "post-tool summary noise\n", encoding="utf-8"
    )

    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge),
        index_path=str(knowledge / "index.sqlite"),
        debounce_ms=0,
        startup_scan=True,
        chunk_tokens=200,
    )
    index = KnowledgeIndex(Path(settings.index_path))
    await index.open()
    try:
        indexer = KnowledgeIndexer(
            index,
            workspace_root=ws,
            knowledge_root=knowledge,
            settings=settings,
            memory_root=memory,
        )
        await indexer.ensure_ready()
        paths = await index.list_paths()
        assert "memory/INDEX.md" in paths
        assert "memory/episodic/tool.md" not in paths
        assert "memory/semantic/echo.md" not in paths
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_full_scan_batches_embeddings_across_files(tmp_path: Path) -> None:
    """F10: one scan should embed in cross-file batches, not one API call per file."""
    from monkeybot.core.persistence.sqlite_vector import SQLiteVectorStore

    ws = tmp_path / "workspace"
    ws.mkdir()
    for i in range(6):
        (ws / f"f{i}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
    knowledge = tmp_path / ".monkeybot" / "knowledge"
    knowledge.mkdir(parents=True)

    class _CountingEmbedder:
        model_id = "fake"
        dim = 2

        def __init__(self) -> None:
            self.calls = 0
            self.batch_sizes: list[int] = []

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            self.batch_sizes.append(len(texts))
            return [[1.0, 0.0] for _ in texts]

        async def embed_query(self, text: str) -> list[float]:
            del text
            return [1.0, 0.0]

    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge),
        index_path=str(knowledge / "index.sqlite"),
        debounce_ms=0,
        startup_scan=True,
        chunk_tokens=200,
    )
    index = KnowledgeIndex(Path(settings.index_path))
    await index.open()
    vectors = SQLiteVectorStore(knowledge / "vectors.sqlite")
    await vectors.open()
    embedder = _CountingEmbedder()
    try:
        indexer = KnowledgeIndexer(
            index,
            workspace_root=ws,
            knowledge_root=knowledge,
            settings=settings,
            embedding_provider=embedder,
            vector_store=vectors,
        )
        await indexer.ensure_ready()
        # Cross-file batching → one embed_documents call for all chunks, not 6
        assert embedder.calls == 1
        assert embedder.batch_sizes[0] >= 6
        for i in range(6):
            assert await vectors.has_path(f"f{i}.py")
    finally:
        await index.close()
        await vectors.close()


@pytest.mark.asyncio
async def test_mtime_fast_path_skips_reread(tmp_path: Path) -> None:
    """F11: unchanged mtime skips read+hash on rescan."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    target = ws / "hello.py"
    target.write_text("def hello():\n    return 1\n", encoding="utf-8")
    knowledge = tmp_path / ".monkeybot" / "knowledge"
    knowledge.mkdir(parents=True)
    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge),
        index_path=str(knowledge / "index.sqlite"),
        debounce_ms=0,
        startup_scan=True,
        chunk_tokens=200,
    )
    index = KnowledgeIndex(Path(settings.index_path))
    await index.open()
    try:
        indexer = KnowledgeIndexer(
            index,
            workspace_root=ws,
            knowledge_root=knowledge,
            settings=settings,
        )
        await indexer.ensure_ready()
        h1 = await index.get_file_hash("hello.py")
        m1 = await index.get_file_mtime("hello.py")
        assert h1 and m1 is not None
        await indexer._full_scan()  # noqa: SLF001
        assert await index.get_file_hash("hello.py") == h1
        assert await index.get_file_mtime("hello.py") == m1
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_readonly_run_command_does_not_rescan(tmp_path: Path) -> None:
    """F11: ls/cat-style commands must not trigger a full workspace rescan."""
    from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
    from monkeybot.core.knowledge.hook import KnowledgeHook

    ws = tmp_path / "workspace"
    ws.mkdir()
    knowledge = tmp_path / ".monkeybot" / "knowledge"
    knowledge.mkdir(parents=True)
    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge),
        index_path=str(knowledge / "index.sqlite"),
        debounce_ms=0,
        startup_scan=False,
    )
    index = KnowledgeIndex(Path(settings.index_path))
    await index.open()
    try:
        indexer = KnowledgeIndexer(
            index,
            workspace_root=ws,
            knowledge_root=knowledge,
            settings=settings,
        )
        await indexer.ensure_ready()
        (ws / "new.py").write_text("x = 1\n", encoding="utf-8")

        hook = KnowledgeHook(indexer)
        mgr = HookManager()
        hook.register(mgr)

        class _Ctx:
            pass

        await mgr.fire(
            HookPayload(
                event=HookEvent.POST_TOOL,
                thread_id="t",
                request_id="r",
                ctx=_Ctx(),  # type: ignore[arg-type]
                tool_name="run_command",
                tool_args={"argv": ["ls", "-la"]},
                tool_result='{"ok": true, "exit_code": 0}',
            )
        )
        await indexer.flush()
        assert "new.py" not in await index.list_paths()
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_flush_continues_after_wave_failure(tmp_path: Path) -> None:
    """One failed embed wave must not discard later paths' vectors."""
    from monkeybot.core.persistence.sqlite_vector import SQLiteVectorStore

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "good_a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (ws / "bad.py").write_text(
        'IMG = "data:image/png;base64,AAAA"\n',
        encoding="utf-8",
    )
    (ws / "good_b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    knowledge = tmp_path / ".monkeybot" / "knowledge"
    knowledge.mkdir(parents=True)

    class _SelectiveEmbedder:
        model_id = "fake"
        dim = 2
        batch_size = 1

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            if any("data:image" in t for t in texts):
                raise RuntimeError("simulated VLM reject")
            return [[1.0, 0.0] for _ in texts]

        async def embed_query(self, text: str) -> list[float]:
            del text
            return [1.0, 0.0]

    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge),
        index_path=str(knowledge / "index.sqlite"),
        debounce_ms=0,
        startup_scan=True,
        chunk_tokens=200,
    )
    index = KnowledgeIndex(Path(settings.index_path))
    await index.open()
    vectors = SQLiteVectorStore(knowledge / "vectors.sqlite")
    await vectors.open()
    try:
        indexer = KnowledgeIndexer(
            index,
            workspace_root=ws,
            knowledge_root=knowledge,
            settings=settings,
            embedding_provider=_SelectiveEmbedder(),
            vector_store=vectors,
        )
        await indexer.ensure_ready()
        assert await vectors.has_path("good_a.py")
        assert await vectors.has_path("good_b.py")
    finally:
        await index.close()
        await vectors.close()


def test_command_implies_fs_mutation_heuristics() -> None:
    from monkeybot.core.knowledge.hook import command_implies_fs_mutation

    assert not command_implies_fs_mutation(["ls", "."])
    assert not command_implies_fs_mutation(["cat", "README.md"])
    assert not command_implies_fs_mutation(["git", "status"])
    assert not command_implies_fs_mutation(["bash", "-c", "ls -la"])
    assert command_implies_fs_mutation(["git", "clone", "url"])
    assert command_implies_fs_mutation(["rm", "-rf", "tmp"])
    assert command_implies_fs_mutation(["pnpm", "install"])
    assert command_implies_fs_mutation(["bash", "-c", "echo hi > out.txt"])
    assert not command_implies_fs_mutation(["mystery-tool"])  # unknown → no rescan
