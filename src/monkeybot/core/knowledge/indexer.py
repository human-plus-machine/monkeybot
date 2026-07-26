"""Scan and incrementally upsert workspace + note files into the knowledge index."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from pathlib import Path

from monkeybot.core.knowledge.captions import resolve_image_caption
from monkeybot.core.knowledge.chunking import CHUNKER_VERSION, chunk_text, index_content_digest
from monkeybot.core.knowledge.embeddings.base import EmbeddingProvider
from monkeybot.core.knowledge.extractors import (
    DOCX_SUFFIXES,
    IMAGE_SUFFIXES,
    PDF_SUFFIXES,
    extract_docx_text,
    extract_pdf_pages,
    media_content_digest,
    read_text_file,
    walk_indexable_files,
)
from monkeybot.core.knowledge.links import parse_wiki_links
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex
from monkeybot.core.knowledge.types import KnowledgeSettings, SourceType, TextChunk
from monkeybot.core.persistence.sqlite_vector import SQLiteVectorStore, VectorChunkRecord

logger = logging.getLogger(__name__)

# Memory is a separate system — never ingest anything under memory/.
_MTIME_EPS = 1e-6


class KnowledgeIndexer:
    """Debounced indexer over workspace files and knowledge notes.

    Process-local dirty queues assume a single gateway writer per workspace index.
    Memory vault paths are never indexed (full split from the memory subsystem).
    """

    def __init__(
        self,
        index: KnowledgeIndex,
        *,
        workspace_root: Path,
        knowledge_root: Path,
        settings: KnowledgeSettings,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: SQLiteVectorStore | None = None,
    ) -> None:
        self._index = index
        self._workspace_root = Path(workspace_root).resolve()
        self._knowledge_root = Path(knowledge_root).resolve()
        self._notes_root = self._knowledge_root / "notes"
        self._settings = settings
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
        """Startup reconciliation. Idempotent.

        When ``startup_scan`` is true, runs a full disk walk (hash skip /
        re-embed on mismatch + ``delete_missing`` for FTS and vectors).

        When ``startup_scan`` is false, the disk walk is skipped — but if a
        vector store is attached, orphan vector rows whose paths are no longer
        in the FTS index are still pruned. Re-embed on content-hash mismatch
        remains gated on a full scan (startup or workspace rescan).
        """
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            await self._purge_stale_model_vectors()
            if self._settings.startup_scan:
                try:
                    await self._full_scan()
                except Exception as exc:
                    logger.warning("knowledge startup scan failed: %r", exc)
                    # Do not mark ready — next ensure_ready/search can retry.
                    return
            elif self._vectors is not None:
                try:
                    await self._prune_orphan_vectors()
                except Exception as exc:
                    logger.warning("knowledge vector orphan prune failed: %r", exc)
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
        """Full workspace + knowledge-notes rescan.

        Invariant: callers must already hold ``self._lock`` (``ensure_ready``
        and ``flush``'s ``do_workspace`` branch both do). ``asyncio.Lock`` is
        not reentrant, so this method itself must never acquire ``self._lock``.
        """
        alive: set[str] = set()
        max_bytes = self._settings.max_file_bytes
        self._pending_embed = []
        try:
            file_paths = await asyncio.to_thread(
                walk_indexable_files, self._workspace_root, max_file_bytes=max_bytes
            )
            for file_path in file_paths:
                rel = _rel_under(file_path, self._workspace_root)
                if rel is None:
                    continue
                await self._index_disk_file(file_path, index_path=rel, source_type="workspace_file")
                alive.add(rel)

            alive |= await self._scan_notes_and_memory(prune=False)
            await self._flush_pending_embeds()
        finally:
            self._pending_embed = None

        # Drop any legacy memory/* rows left from before the full split.
        existing_notes = await self._index.list_paths(source_type="note")
        for path in existing_notes:
            if path.startswith("memory/"):
                await self._index.delete_path(path)
                await self._delete_vectors(path)

        deleted = await self._index.delete_missing(alive)
        if deleted:
            logger.info("knowledge index removed %d stale paths", deleted)
        try:
            await self._prune_orphan_vectors(alive)
        except Exception as exc:
            logger.warning("knowledge vector prune failed: %r", exc)

    async def _scan_notes_and_memory(self, *, prune: bool = True) -> set[str]:
        """Index knowledge notes only (memory vault is never ingested)."""
        alive: set[str] = set()
        max_bytes = self._settings.max_file_bytes
        owns_batch = self._pending_embed is None
        if owns_batch:
            self._pending_embed = []

        try:
            self._notes_root.mkdir(parents=True, exist_ok=True)
            note_files = await asyncio.to_thread(
                walk_indexable_files, self._notes_root, max_file_bytes=max_bytes
            )
            for file_path in note_files:
                rel = _rel_under(file_path, self._notes_root)
                if rel is None:
                    continue
                index_path = f"notes/{rel}"
                await self._index_disk_file(file_path, index_path=index_path, source_type="note")
                alive.add(index_path)

            # Prune any leftover memory/* note rows on notes rescan.
            existing_notes = await self._index.list_paths(source_type="note")
            for path in existing_notes:
                if path.startswith("memory/"):
                    await self._index.delete_path(path)
                    await self._delete_vectors(path)

            if owns_batch:
                await self._flush_pending_embeds()
        finally:
            if owns_batch:
                self._pending_embed = None

        if prune:
            existing_notes = await self._index.list_paths(source_type="note")
            for path in existing_notes - alive:
                if path.startswith("memory/"):
                    continue  # already deleted above
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
            # Full split: never keep memory paths in the knowledge index.
            await self._index.delete_path(path)
            await self._delete_vectors(path)
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

        # F11: mtime fast path — skip read+hash when unchanged. Files chunked by
        # an older CHUNKER_VERSION never take it, so a version bump re-chunks
        # existing workspaces instead of waiting for each file to be touched.
        state = await self._index.get_file_state(index_path)
        if (
            state is not None
            and state.chunker_version == CHUNKER_VERSION
            and state.mtime is not None
            and abs(state.mtime - float(mtime)) < _MTIME_EPS
        ):
            if self._embedder is None or self._vectors is None:
                return
            try:
                if await self._vectors.has_path(index_path):
                    return
            except Exception as exc:
                logger.warning("knowledge vector has_path failed for %s: %r", index_path, exc)
            # Need embed backfill — fall through to read.

        suffix = file_path.suffix.lower()
        max_bytes = self._settings.max_file_bytes

        if suffix in PDF_SUFFIXES:
            await self._index_pdf(
                file_path, index_path=index_path, source_type=source_type, mtime=mtime
            )
            return
        if suffix in DOCX_SUFFIXES:
            text = await asyncio.to_thread(extract_docx_text, file_path, max_file_bytes=max_bytes)
            if text is None:
                await self._index.delete_path(index_path)
                await self._delete_vectors(index_path)
                return
            digest = await asyncio.to_thread(_media_digest_from_path, file_path)
            await self._index_text(
                text,
                index_path=index_path,
                source_type=source_type,
                mtime=mtime,
                content_digest=digest,
            )
            return
        if suffix in IMAGE_SUFFIXES:
            await self._index_image(
                file_path, index_path=index_path, source_type=source_type, mtime=mtime
            )
            return

        text = await asyncio.to_thread(read_text_file, file_path, max_file_bytes=max_bytes)
        if text is None:
            await self._index.delete_path(index_path)
            await self._delete_vectors(index_path)
            return
        await self._index_text(text, index_path=index_path, source_type=source_type, mtime=mtime)

    async def _index_pdf(
        self,
        file_path: Path,
        *,
        index_path: str,
        source_type: SourceType,
        mtime: float | None,
    ) -> None:
        pages = await asyncio.to_thread(
            extract_pdf_pages,
            file_path,
            max_file_bytes=self._settings.max_file_bytes,
        )
        if not pages:
            await self._index.delete_path(index_path)
            await self._delete_vectors(index_path)
            return
        digest = await asyncio.to_thread(_media_digest_from_path, file_path)
        existing = await self._index.get_file_hash(index_path)
        if existing == digest:
            joined = "\n\n".join(f"[PDF page {p.page}]\n{p.text}" for p in pages)
            await self._maybe_backfill_embeddings(
                joined, index_path=index_path, source_type=source_type
            )
            return
        chunks = [
            TextChunk(
                path=index_path,
                source_type=source_type,
                start_line=page.page,
                end_line=page.page,
                text=f"[PDF page {page.page}]\n{page.text}",
            )
            for page in pages
        ]
        await self._index.upsert_file(
            path=index_path,
            source_type=source_type,
            content_hash=digest,
            mtime=mtime,
            chunks=chunks,
            links=[],
        )
        await self._embed_chunks(chunks)

    async def _index_image(
        self,
        file_path: Path,
        *,
        index_path: str,
        source_type: SourceType,
        mtime: float | None,
    ) -> None:
        mode = self._settings.captions
        if mode == "off":
            await self._index.delete_path(index_path)
            await self._delete_vectors(index_path)
            return
        cache_dir = self._knowledge_root / "captions"
        caption = await resolve_image_caption(
            rel_path=index_path,
            file_path=file_path,
            mode=mode,
            cache_dir=cache_dir,
            caption_model=self._settings.caption_model,
        )
        if caption is None:
            await self._index.delete_path(index_path)
            await self._delete_vectors(index_path)
            return
        digest = await asyncio.to_thread(_media_digest_from_path, file_path)
        # Mix caption into digest so caption-mode changes force re-index.
        caption_digest = media_content_digest(
            f"{digest}\n{mode}\n{caption}".encode(),
            chunker_version=CHUNKER_VERSION,
        )
        await self._index_text(
            caption,
            index_path=index_path,
            source_type=source_type,
            mtime=mtime,
            content_digest=caption_digest,
        )

    async def _index_text(
        self,
        text: str,
        *,
        index_path: str,
        source_type: SourceType,
        mtime: float | None,
        content_digest: str | None = None,
    ) -> None:
        if content_digest is not None:
            digest = content_digest
        else:
            digest = await asyncio.to_thread(index_content_digest, text)
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
        links = parse_wiki_links(text, source_path=index_path) if source_type == "note" else []
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
        # Reuse persisted FTS chunk boundaries so vector IDs/spans stay aligned
        # even if chunk_tokens / overlap_ratio changed since the last index.
        chunks = await self._index.list_chunks_for_path(index_path)
        if not chunks:
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
                    "knowledge embed wave failed (%d chunks at offset %d): %r; retrying per path",
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

    async def _purge_stale_model_vectors(self) -> None:
        """Drop vectors from a previous embedding model / dimension config.

        Vectors are only comparable within one model+width, so a provider or
        ``dimensions`` change must evict the old rows; the scan that follows
        re-embeds those paths with the active model.
        """
        if self._embedder is None or self._vectors is None:
            return
        try:
            await self._vectors.delete_stale_models(
                self._embedder.model_id, self._embedder.dim
            )
        except Exception as exc:
            logger.warning("knowledge stale-model vector purge failed: %r", exc)

    async def _prune_orphan_vectors(self, alive: set[str] | None = None) -> None:
        """Drop vector rows for paths absent from the FTS index (or ``alive``)."""
        if self._vectors is None:
            return
        if alive is None:
            alive = await self._index.list_paths()
        deleted = await self._vectors.delete_missing(alive)
        if deleted:
            logger.info("knowledge vectors removed %d stale paths", deleted)

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


def _media_digest_from_path(file_path: Path) -> str:
    raw = file_path.read_bytes()
    return media_content_digest(raw, chunker_version=CHUNKER_VERSION)


def _looks_like_text_name(name: str) -> bool:
    lower = name.lower()
    return lower.endswith((".md", ".txt", ".markdown", ".rst", ".json", ".yaml", ".yml"))


__all__ = ["KnowledgeIndexer"]
