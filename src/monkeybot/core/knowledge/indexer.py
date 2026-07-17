"""Scan and incrementally upsert workspace + note files into the knowledge index."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from pathlib import Path

from monkeybot.core.knowledge.chunking import chunk_text
from monkeybot.core.knowledge.embeddings.nvidia import NvidiaEmbeddingProvider
from monkeybot.core.knowledge.extractors import content_hash, read_text_file, walk_text_files
from monkeybot.core.knowledge.links import parse_wiki_links
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex
from monkeybot.core.knowledge.types import KnowledgeSettings, SourceType, TextChunk
from monkeybot.core.persistence.sqlite_vector import SQLiteVectorStore, VectorChunkRecord
from monkeybot.core.workspace.protocol import WorkspaceStorage

logger = logging.getLogger(__name__)

# Memory vault paths we skip (noise / auto-capture / organizer archives).
# Index only INDEX.md and curated notes under knowledge/notes/.
_MEMORY_SKIP_PREFIXES = ("raw/", "episodic/", "semantic/")
_MTIME_EPS = 1e-6


class KnowledgeIndexer:
    """Debounced indexer over workspace files, knowledge notes, and memory vault.

    Process-local dirty queues assume a single gateway writer per workspace index.
    """

    def __init__(
        self,
        index: KnowledgeIndex,
        *,
        workspace_root: Path,
        knowledge_root: Path,
        settings: KnowledgeSettings,
        memory_storage: WorkspaceStorage | None = None,
        memory_root: Path | None = None,
        embedding_provider: NvidiaEmbeddingProvider | None = None,
        vector_store: SQLiteVectorStore | None = None,
    ) -> None:
        self._index = index
        self._workspace_root = Path(workspace_root).resolve()
        self._knowledge_root = Path(knowledge_root).resolve()
        self._notes_root = self._knowledge_root / "notes"
        self._settings = settings
        self._memory_storage = memory_storage
        self._memory_root = Path(memory_root).resolve() if memory_root else None
        self._embedder = embedding_provider
        self._vectors = vector_store
        self._dirty: set[tuple[str, SourceType | None]] = set()
        self._notes_rescan = False
        self._workspace_rescan = False
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None
        self._ready = False
        # When set, `_embed_chunks` appends instead of calling the API (F10).
        self._pending_embed: list[TextChunk] | None = None

    @property
    def ready(self) -> bool:
        return self._ready

    async def ensure_ready(self) -> None:
        """Startup full scan (when configured). Idempotent."""
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            if self._settings.startup_scan:
                try:
                    await self._full_scan()
                except Exception as exc:
                    logger.warning("knowledge startup scan failed: %r", exc)
                    # Do not mark ready — next ensure_ready/search can retry.
                    return
            self._ready = True

    def enqueue(self, path: str, *, source_type: SourceType | None = None) -> None:
        """Schedule a path for reindex (debounced). ``path`` is index-relative."""
        self._dirty.add((path, source_type))
        self._schedule_flush()

    def enqueue_workspace_rel(self, rel: str) -> None:
        """Enqueue a workspace-relative path as ``workspace_file``."""
        cleaned = rel.strip().lstrip("./")
        if cleaned:
            self.enqueue(cleaned, source_type="workspace_file")

    def request_notes_rescan(self) -> None:
        """Debounced re-walk of knowledge notes + memory vault (not full workspace)."""
        self._notes_rescan = True
        self._schedule_flush()

    def request_workspace_rescan(self) -> None:
        """Debounced full workspace (+ notes/memory) rescan.

        Used after shell mutations (``run_command`` / ``git clone``) that bypass
        file-write tools.
        """
        self._workspace_rescan = True
        self._schedule_flush()

    def _has_pending_work(self) -> bool:
        return bool(self._dirty) or self._notes_rescan or self._workspace_rescan

    def _schedule_flush(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return

        async def _run() -> None:
            delay = max(0, self._settings.debounce_ms) / 1000.0
            await asyncio.sleep(delay)
            # Drain until idle so work enqueued during a flush is not dropped.
            while True:
                try:
                    await self.flush()
                except Exception as exc:
                    logger.warning("knowledge flush failed: %r", exc)
                if not self._has_pending_work():
                    break
                if delay > 0:
                    await asyncio.sleep(delay)

        try:
            self._flush_task = asyncio.create_task(_run())
        except RuntimeError:
            # No running loop (rare in tests) — flush synchronously later via flush()
            self._flush_task = None

    async def flush(self) -> None:
        """Process the dirty queue immediately."""
        async with self._lock:
            pending = list(self._dirty)
            self._dirty.clear()
            do_workspace = self._workspace_rescan
            self._workspace_rescan = False
            do_notes = self._notes_rescan and not do_workspace
            self._notes_rescan = False

            if do_workspace:
                try:
                    await self._full_scan()
                except Exception as exc:
                    logger.warning("knowledge workspace rescan failed: %r", exc)
                return

            for path, source_type in pending:
                try:
                    await self._reindex_one(path, source_type)
                except Exception as exc:
                    logger.warning("knowledge reindex failed for %s: %r", path, exc)
            if do_notes:
                try:
                    await self._scan_notes_and_memory()
                except Exception as exc:
                    logger.warning("knowledge notes rescan failed: %r", exc)

    async def _full_scan(self) -> None:
        alive: set[str] = set()
        max_bytes = self._settings.max_file_bytes
        self._pending_embed = []
        try:
            file_paths = await asyncio.to_thread(
                walk_text_files, self._workspace_root, max_file_bytes=max_bytes
            )
            for file_path in file_paths:
                rel = _rel_under(file_path, self._workspace_root)
                if rel is None:
                    continue
                await self._index_disk_file(
                    file_path, index_path=rel, source_type="workspace_file"
                )
                alive.add(rel)

            alive |= await self._scan_notes_and_memory(prune=False)
            await self._flush_pending_embeds()
        finally:
            self._pending_embed = None

        deleted = await self._index.delete_missing(alive)
        if deleted:
            logger.info("knowledge index removed %d stale paths", deleted)
        if self._vectors is not None:
            try:
                v_deleted = await self._vectors.delete_missing(alive)
                if v_deleted:
                    logger.info("knowledge vectors removed %d stale paths", v_deleted)
            except Exception as exc:
                logger.warning("knowledge vector prune failed: %r", exc)

    async def _scan_notes_and_memory(self, *, prune: bool = True) -> set[str]:
        """Index knowledge notes + memory vault. Returns alive note paths."""
        alive: set[str] = set()
        max_bytes = self._settings.max_file_bytes
        owns_batch = self._pending_embed is None
        if owns_batch:
            self._pending_embed = []

        try:
            self._notes_root.mkdir(parents=True, exist_ok=True)
            note_files = await asyncio.to_thread(
                walk_text_files, self._notes_root, max_file_bytes=max_bytes
            )
            for file_path in note_files:
                rel = _rel_under(file_path, self._notes_root)
                if rel is None:
                    continue
                index_path = f"notes/{rel}"
                await self._index_disk_file(
                    file_path, index_path=index_path, source_type="note"
                )
                alive.add(index_path)

            if self._memory_root is not None and self._memory_root.is_dir():
                mem_files = await asyncio.to_thread(
                    walk_text_files, self._memory_root, max_file_bytes=max_bytes
                )
                for file_path in mem_files:
                    rel = _rel_under(file_path, self._memory_root)
                    if rel is None or _memory_skipped(rel):
                        continue
                    index_path = f"memory/{rel}"
                    await self._index_disk_file(
                        file_path, index_path=index_path, source_type="note"
                    )
                    alive.add(index_path)
            elif self._memory_storage is not None:
                try:
                    names = await self._memory_storage.list_files()
                except Exception as exc:
                    logger.warning("knowledge memory list_files failed: %r", exc)
                    names = []
                for name in names:
                    rel = name.lstrip("/")
                    if _memory_skipped(rel) or not _looks_like_text_name(rel):
                        continue
                    index_path = f"memory/{rel}"
                    try:
                        text = await self._memory_storage.read_text(rel)
                    except Exception as exc:
                        logger.warning(
                            "knowledge memory read_text failed for %s: %r", rel, exc
                        )
                        continue
                    await self._index_text(
                        text,
                        index_path=index_path,
                        source_type="note",
                        mtime=None,
                    )
                    alive.add(index_path)

            if owns_batch:
                await self._flush_pending_embeds()
        finally:
            if owns_batch:
                self._pending_embed = None

        if prune:
            existing_notes = await self._index.list_paths(source_type="note")
            for path in existing_notes - alive:
                await self._index.delete_path(path)
                await self._delete_vectors(path)
        return alive

    async def _reindex_one(self, path: str, source_type: SourceType | None) -> None:
        if source_type == "workspace_file" or (
            source_type is None and not path.startswith(("notes/", "memory/"))
        ):
            disk = self._workspace_root / path
            if not disk.is_file():
                await self._index.delete_path(path)
                await self._delete_vectors(path)
                return
            await self._index_disk_file(disk, index_path=path, source_type="workspace_file")
            return

        if path.startswith("notes/"):
            rel = path[len("notes/") :]
            disk = self._notes_root / rel
            if not disk.is_file():
                await self._index.delete_path(path)
                await self._delete_vectors(path)
                return
            await self._index_disk_file(disk, index_path=path, source_type="note")
            return

        if path.startswith("memory/"):
            rel = path[len("memory/") :]
            if _memory_skipped(rel):
                await self._index.delete_path(path)
                await self._delete_vectors(path)
                return
            if self._memory_root is not None:
                disk = self._memory_root / rel
                if not disk.is_file():
                    await self._index.delete_path(path)
                    await self._delete_vectors(path)
                    return
                await self._index_disk_file(disk, index_path=path, source_type="note")
                return
            if self._memory_storage is not None:
                try:
                    text = await self._memory_storage.read_text(rel)
                except Exception as exc:
                    logger.warning(
                        "knowledge memory read failed for %s; deleting index row: %r",
                        path,
                        exc,
                    )
                    await self._index.delete_path(path)
                    await self._delete_vectors(path)
                    return
                await self._index_text(
                    text, index_path=path, source_type="note", mtime=None
                )
                return
            await self._index.delete_path(path)
            await self._delete_vectors(path)

    async def _index_disk_file(
        self,
        file_path: Path,
        *,
        index_path: str,
        source_type: SourceType,
    ) -> None:
        try:
            mtime = await asyncio.to_thread(lambda: file_path.stat().st_mtime)
        except OSError:
            mtime = time.time()

        # F11: mtime fast path — skip read+hash when unchanged.
        stored_mtime = await self._index.get_file_mtime(index_path)
        if (
            stored_mtime is not None
            and abs(float(stored_mtime) - float(mtime)) < _MTIME_EPS
        ):
            if self._embedder is None or self._vectors is None:
                return
            try:
                if await self._vectors.has_path(index_path):
                    return
            except Exception as exc:
                logger.warning(
                    "knowledge vector has_path failed for %s: %r", index_path, exc
                )
            # Need embed backfill — fall through to read.

        text = await asyncio.to_thread(
            read_text_file, file_path, max_file_bytes=self._settings.max_file_bytes
        )
        if text is None:
            await self._index.delete_path(index_path)
            await self._delete_vectors(index_path)
            return
        await self._index_text(
            text, index_path=index_path, source_type=source_type, mtime=mtime
        )

    async def _index_text(
        self,
        text: str,
        *,
        index_path: str,
        source_type: SourceType,
        mtime: float | None,
    ) -> None:
        digest = await asyncio.to_thread(content_hash, text)
        existing = await self._index.get_file_hash(index_path)
        if existing == digest:
            # FTS is current, but embeddings may still be missing (ANN just enabled).
            await self._maybe_backfill_embeddings(
                text, index_path=index_path, source_type=source_type
            )
            return

        chunks = chunk_text(
            text,
            path=index_path,
            source_type=source_type,
            chunk_tokens=self._settings.chunk_tokens,
            overlap_ratio=self._settings.chunk_overlap_ratio,
        )
        links = (
            parse_wiki_links(text, source_path=index_path)
            if source_type == "note"
            else []
        )
        await self._index.upsert_file(
            path=index_path,
            source_type=source_type,
            content_hash=digest,
            mtime=mtime,
            chunks=chunks,
            links=links,
        )
        await self._embed_chunks(chunks)

    async def _maybe_backfill_embeddings(
        self,
        text: str,
        *,
        index_path: str,
        source_type: SourceType,
    ) -> None:
        if self._embedder is None or self._vectors is None:
            return
        try:
            if await self._vectors.has_path(index_path):
                return
        except Exception as exc:
            logger.warning("knowledge vector has_path failed for %s: %r", index_path, exc)
            return
        chunks = chunk_text(
            text,
            path=index_path,
            source_type=source_type,
            chunk_tokens=self._settings.chunk_tokens,
            overlap_ratio=self._settings.chunk_overlap_ratio,
        )
        await self._embed_chunks(chunks)

    async def _embed_chunks(self, chunks: list[TextChunk]) -> None:
        if not chunks or self._embedder is None or self._vectors is None:
            return
        if self._pending_embed is not None:
            self._pending_embed.extend(chunks)
            return
        # Incremental single-file path (hook-driven updates).
        path = chunks[0].path
        try:
            await self._vectors.delete_by_path(path)
            texts = [c.text for c in chunks]
            vectors = await self._embedder.embed_documents(texts)
            records = [
                VectorChunkRecord(
                    chunk_id=chunk.chunk_id,
                    path=chunk.path,
                    vector=vec,
                    model_id=self._embedder.model_id,
                    dim=self._embedder.dim,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    source_type=chunk.source_type,
                    text=chunk.text,
                )
                for chunk, vec in zip(chunks, vectors, strict=True)
            ]
            await self._vectors.upsert(records)
        except Exception as exc:
            logger.warning("knowledge embed upsert failed for %s: %r", path, exc)

    async def _flush_pending_embeds(self) -> None:
        """Embed accumulated scan chunks in cross-file waves (F10).

        Upserts after each wave so a mid-scan API failure cannot discard all
        progress. Per-path fallback when a wave fails entirely.
        """
        chunks = list(self._pending_embed or [])
        if self._pending_embed is not None:
            self._pending_embed.clear()
        if not chunks or self._embedder is None or self._vectors is None:
            return

        by_path: dict[str, list[TextChunk]] = defaultdict(list)
        for chunk in chunks:
            by_path[chunk.path].append(chunk)

        flat: list[TextChunk] = []
        for path_chunks in by_path.values():
            flat.extend(path_chunks)

        batch_size = max(1, int(getattr(self._embedder, "batch_size", 32) or 32))
        wave_size = max(batch_size, batch_size * 4)
        ok = 0
        for start in range(0, len(flat), wave_size):
            wave = flat[start : start + wave_size]
            try:
                await self._embed_upsert_chunks(wave)
                ok += len(wave)
            except Exception as exc:
                logger.warning(
                    "knowledge embed wave failed (%d chunks at offset %d): %r; "
                    "retrying per path",
                    len(wave),
                    start,
                    exc,
                )
                paths_in_wave: dict[str, list[TextChunk]] = defaultdict(list)
                for chunk in wave:
                    paths_in_wave[chunk.path].append(chunk)
                for path, path_chunks in paths_in_wave.items():
                    try:
                        await self._embed_upsert_chunks(path_chunks)
                        ok += len(path_chunks)
                    except Exception as path_exc:
                        logger.warning(
                            "knowledge embed upsert failed for %s: %r",
                            path,
                            path_exc,
                        )
        if ok:
            logger.info(
                "knowledge embedded %d/%d chunks across %d paths",
                ok,
                len(flat),
                len(by_path),
            )

    async def _embed_upsert_chunks(self, chunks: list[TextChunk]) -> None:
        """Embed a list of chunks and upsert vectors, replacing prior rows per path."""
        assert self._embedder is not None and self._vectors is not None
        if not chunks:
            return
        texts = [c.text for c in chunks]
        vectors = await self._embedder.embed_documents(texts)
        records_by_path: dict[str, list[VectorChunkRecord]] = defaultdict(list)
        for chunk, vec in zip(chunks, vectors, strict=True):
            records_by_path[chunk.path].append(
                VectorChunkRecord(
                    chunk_id=chunk.chunk_id,
                    path=chunk.path,
                    vector=vec,
                    model_id=self._embedder.model_id,
                    dim=self._embedder.dim,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    source_type=chunk.source_type,
                    text=chunk.text,
                )
            )
        for path, records in records_by_path.items():
            await self._vectors.delete_by_path(path)
            await self._vectors.upsert(records)

    async def _delete_vectors(self, path: str) -> None:
        if self._vectors is None:
            return
        try:
            await self._vectors.delete_by_path(path)
        except Exception as exc:
            logger.warning("knowledge vector delete failed for %s: %r", path, exc)


def _rel_under(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _memory_skipped(rel: str) -> bool:
    return any(rel.startswith(p) for p in _MEMORY_SKIP_PREFIXES)


def _looks_like_text_name(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(
        (".md", ".txt", ".markdown", ".rst", ".json", ".yaml", ".yml")
    )


__all__ = ["KnowledgeIndexer"]
