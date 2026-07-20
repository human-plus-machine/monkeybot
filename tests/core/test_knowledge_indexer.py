"""Tests for KnowledgeIndexer scan / hash skip / ignore dirs."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from monkeybot.core.knowledge.indexer import KnowledgeIndexer
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex
from monkeybot.core.knowledge.types import KnowledgeSettings
from monkeybot.core.workspace.protocol import WorkspaceStorage


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
async def test_indexer_skips_all_memory(tmp_path: Path) -> None:
    """Full split: nothing under memory/ is indexed (including INDEX.md)."""
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
        )
        await indexer.ensure_ready()
        paths = await index.list_paths()
        assert "memory/INDEX.md" not in paths
        assert "memory/episodic/tool.md" not in paths
        assert "memory/semantic/echo.md" not in paths
        assert not any(p.startswith("memory/") for p in paths)
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_indexer_skips_episodic_and_semantic_memory(tmp_path: Path) -> None:
    await test_indexer_skips_all_memory(tmp_path)

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


class _FlakyStorage:
    """FakeWorkspaceStorage wrapper that raises transient errors on demand."""

    def __init__(self, inner: WorkspaceStorage) -> None:
        self._inner = inner
        self.list_files_fail = False
        self.read_text_fail_paths: set[str] = set()

    async def read_text(self, path: str) -> str:
        if path in self.read_text_fail_paths:
            raise TimeoutError(f"simulated transient read failure for {path}")
        return cast(str, await self._inner.read_text(path))

    async def list_files(self, prefix: str = "") -> list[str]:
        if self.list_files_fail:
            raise TimeoutError("simulated transient list_files failure")
        return cast("list[str]", await self._inner.list_files(prefix))

    async def write_text(self, path: str, content: str) -> None:
        await self._inner.write_text(path, content)

    async def append_text(self, path: str, content: str) -> None:
        await self._inner.append_text(path, content)

    async def exists(self, path: str) -> bool:
        return cast(bool, await self._inner.exists(path))

    async def delete(self, path: str) -> None:
        await self._inner.delete(path)

    async def move(self, src: str, dest: str) -> None:
        await self._inner.move(src, dest)

    async def gc_prefix(self, prefix: str, max_age_sec: float) -> dict[str, int]:
        return cast("dict[str, int]", await self._inner.gc_prefix(prefix, max_age_sec))


@pytest.mark.asyncio
async def test_reindex_one_deletes_memory_paths_on_full_split(
    tmp_path: Path,
) -> None:
    """Full split: reindexing a memory/* path deletes it (never kept)."""
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
        chunk_tokens=200,
    )
    index = KnowledgeIndex(Path(settings.index_path))
    await index.open()
    try:
        await index.upsert_file(
            path="memory/note.md",
            source_type="note",
            content_hash="abc",
            mtime=1.0,
            chunks=[],
            links=[],
        )
        assert "memory/note.md" in await index.list_paths()

        indexer = KnowledgeIndexer(
            index,
            workspace_root=ws,
            knowledge_root=knowledge,
            settings=settings,
        )
        await indexer._reindex_one("memory/note.md", "note")  # noqa: SLF001
        assert "memory/note.md" not in await index.list_paths()
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_reindex_one_keeps_stale_row_on_transient_memory_read_error(
    tmp_path: Path,
) -> None:
    """Legacy name: memory paths are deleted under full split."""
    await test_reindex_one_deletes_memory_paths_on_full_split(tmp_path)


@pytest.mark.asyncio
async def test_reindex_one_deletes_on_confirmed_missing_memory_note(
    tmp_path: Path,
) -> None:
    await test_reindex_one_deletes_memory_paths_on_full_split(tmp_path)


@pytest.mark.asyncio
async def test_full_scan_prunes_legacy_memory_notes(
    tmp_path: Path,
) -> None:
    """Full scan drops leftover memory/* rows."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "app.py").write_text("print('hi')\n", encoding="utf-8")
    knowledge = tmp_path / ".monkeybot" / "knowledge"
    knowledge.mkdir(parents=True)

    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge),
        index_path=str(knowledge / "index.sqlite"),
        debounce_ms=0,
        startup_scan=False,
        chunk_tokens=200,
    )
    index = KnowledgeIndex(Path(settings.index_path))
    await index.open()
    try:
        await index.upsert_file(
            path="memory/note.md",
            source_type="note",
            content_hash="abc",
            mtime=1.0,
            chunks=[],
            links=[],
        )
        indexer = KnowledgeIndexer(
            index,
            workspace_root=ws,
            knowledge_root=knowledge,
            settings=settings,
        )
        await indexer._full_scan()  # noqa: SLF001
        assert "memory/note.md" not in await index.list_paths()
        assert "app.py" in await index.list_paths()
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_full_scan_keeps_memory_notes_alive_on_list_files_failure(
    tmp_path: Path,
) -> None:
    """Legacy name: full scan now prunes memory notes (full split)."""
    await test_full_scan_prunes_legacy_memory_notes(tmp_path)


def test_command_implies_fs_mutation_heuristics() -> None:
    from monkeybot.core.knowledge.hook import command_implies_fs_mutation

    assert not command_implies_fs_mutation(["ls", "."])
    assert not command_implies_fs_mutation(["cat", "README.md"])
    assert not command_implies_fs_mutation(["git", "status"])
    assert not command_implies_fs_mutation(["bash", "-c", "ls -la"])
    assert not command_implies_fs_mutation(["git", "stash", "list"])
    assert not command_implies_fs_mutation(["find", ".", "-name", "*.py"])
    assert command_implies_fs_mutation(["git", "clone", "url"])
    assert command_implies_fs_mutation(["rm", "-rf", "tmp"])
    assert command_implies_fs_mutation(["pnpm", "install"])
    assert command_implies_fs_mutation(["bash", "-c", "echo hi > out.txt"])
    assert command_implies_fs_mutation(["bash", "-c", "cat foo && rm -rf bar"])
    assert command_implies_fs_mutation(["bash", "-c", "ls; touch new.txt"])
    assert command_implies_fs_mutation(["find", ".", "-delete"])
    assert command_implies_fs_mutation(["find", ".", "-exec", "rm", "{}", ";"])
    assert command_implies_fs_mutation(["git", "stash"])
    assert command_implies_fs_mutation(["git", "stash", "pop"])
    assert command_implies_fs_mutation(["sudo", "rm", "-rf", "tmp"])
    assert command_implies_fs_mutation(["env", "FOO=1", "rm", "-rf", "tmp"])
    assert command_implies_fs_mutation(["bash", "-c", "cmd 2>/tmp/out.txt"])
    assert command_implies_fs_mutation(["python3", "-c", "open('x','w').write('y')"])
    assert command_implies_fs_mutation(["mystery-tool"])  # unknown → rescan (fail-safe)
