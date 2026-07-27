"""Single injection point for memory (storage + hook + organizer + graph)."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import asyncio

from monkeybot.core.hooks import HookManager
from monkeybot.core.llm.provider import Provider
from monkeybot.core.memory.graph import MemoryGraph, MemoryGraphStore
from monkeybot.core.memory.hook import MemoryHook
from monkeybot.core.memory.mutate import (
    drop_index_paths,
    edit_memory_note,
    forget_memory_note,
    update_memory_note,
)
from monkeybot.core.memory.note_format import (
    TYPED_FOLDERS,
    extract_memory_wiki_links,
    folder_from_rel_path,
    parse_memory_note,
)
from monkeybot.core.memory.organizer import MemoryOrganizer
from monkeybot.core.memory.repair import repair_memory_tree
from monkeybot.core.memory.storage_ops import (
    async_load_index,
    async_load_memory_hit,
    async_promote_to_memory,
    async_search_memory_files,
)
from monkeybot.core.workspace.protocol import WorkspaceStorage

logger = logging.getLogger(__name__)


def _local_memory_root(memory_uri: str) -> Path | None:
    raw = memory_uri.strip()
    if raw.startswith("local://"):
        path = raw[len("local://") :]
        return Path(path).expanduser().resolve()
    parsed = urlparse(raw)
    if parsed.scheme in ("", "file"):
        return Path(parsed.path or raw).expanduser().resolve()
    return None


def _working_ttl_days() -> float:
    raw = os.environ.get("MEMORY_WORKING_TTL_DAYS", "7").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 7.0


class MemorySubsystem:
    """Owns storage, hook, organizer, and the memory graph sidecar."""

    def __init__(
        self,
        storage: WorkspaceStorage,
        provider: Provider,
        model: str,
        *,
        memory_uri: str,
        max_retrieval_hits: int = 3,
        graph: MemoryGraphStore | None = None,
    ) -> None:
        self._storage = storage
        self._memory_uri = memory_uri.strip()
        self._hook = MemoryHook(
            storage=storage,
            organizer_runner=self._run_organizer_locked,
            max_retrieval_hits=max_retrieval_hits,
        )
        root = _local_memory_root(memory_uri)
        if graph is not None:
            self._graph: MemoryGraphStore | None = graph
        elif root is not None:
            self._graph = MemoryGraph(root / ".graph.sqlite")
        else:
            # Remote/shared backends need a shared graph store first.
            self._graph = None
            logger.info(
                "memory graph disabled until shared storage exists (uri scheme non-local)"
            )
        self._graph_opened = False
        self._organizer = MemoryOrganizer(
            provider=provider,
            model=model,
            storage=storage,
            on_note_written=self._on_note_written,
            pre_run=self.gc_working,
        )

    @property
    def uri(self) -> str:
        return self._memory_uri

    @property
    def storage(self) -> WorkspaceStorage:
        return self._storage

    @property
    def lock(self) -> asyncio.Lock:
        return self._hook._lock  # noqa: SLF001 — shared with mutation tools

    def register_hooks(self, manager: HookManager) -> None:
        self._hook.register(manager)

    async def ensure_graph(self) -> MemoryGraphStore | None:
        """Open the graph sidecar; return None if unavailable (best-effort)."""
        if self._graph is None:
            return None
        if not self._graph_opened:
            try:
                await self._graph.open()
                self._graph_opened = True
            except Exception as exc:
                logger.warning("memory graph open failed (continuing without graph): %r", exc)
                return None
        return self._graph

    async def _on_note_written(self, path: str, text: str) -> None:
        graph = await self.ensure_graph()
        if graph is None:
            return
        try:
            meta, _ = parse_memory_note(text)
            note_type = meta.type if meta is not None else (folder_from_rel_path(path) or "episodic")
            status = meta.status if meta is not None else "active"
            supersedes = meta.supersedes if meta is not None else None
            links = [(t, "related") for t in extract_memory_wiki_links(text)]
            if supersedes:
                links.append((supersedes, "supersedes"))
            await graph.upsert_note(
                path,
                note_type=note_type,
                status=status,
                updated_at=time.time(),
                links=links,
            )
        except Exception as exc:
            logger.debug("memory graph note upsert skipped path=%s: %r", path, exc)

    async def rebuild_graph(self) -> dict[str, int]:
        """Scan typed memory folders and upsert every note into the sidecar graph.

        Used on gateway startup (and when the viz finds an empty graph) so notes
        filed before the graph existed still appear in the UI.
        """
        graph = await self.ensure_graph()
        if graph is None:
            return {"scanned": 0, "upserted": 0, "errors": 0, "skipped": 1}
        scanned = 0
        upserted = 0
        errors = 0
        for folder in TYPED_FOLDERS:
            try:
                paths = await self._storage.list_files(f"{folder}/")
            except Exception as exc:
                logger.warning("rebuild_graph list %s/ failed: %r", folder, exc)
                errors += 1
                continue
            for rel in paths:
                rel_posix = rel.replace("\\", "/")
                if not rel_posix.startswith(f"{folder}/") or not rel_posix.endswith(".md"):
                    continue
                if rel_posix.count("/") != 1:
                    continue
                scanned += 1
                try:
                    text = await self._storage.read_text(rel_posix)
                    await self._on_note_written(rel_posix, text)
                    upserted += 1
                except Exception as exc:
                    logger.warning("rebuild_graph failed for %s: %r", rel_posix, exc)
                    errors += 1
        return {"scanned": scanned, "upserted": upserted, "errors": errors}

    async def _run_organizer_locked(self) -> Any:
        async with self.lock:
            return await self._organizer.run()

    async def load_index(self) -> list[str]:
        # Repair corruption first, then load. Do not swallow load failures here —
        # refresh_memory_index relies on exceptions to keep a stale index on
        # transient storage errors (OSError, etc.).
        report = await repair_memory_tree(self._storage)
        if report.quarantined or report.index_rebuilt or report.index_pruned:
            logger.warning(
                "memory repair applied uri=%s quarantined=%s rebuilt=%s pruned=%s entries=%s",
                self._memory_uri,
                report.quarantined,
                report.index_rebuilt,
                report.index_pruned,
                report.entries_written,
            )
        lines = await async_load_index(self._storage)
        # Drop working/ entries from the prompt window (demotion).
        filtered: list[str] = []
        for line in lines:
            if "[[working/" in line.replace(" ", ""):
                continue
            filtered.append(line)
        return filtered

    async def search_files(
        self,
        query: str,
        *,
        max_hits: int = 40,
        skip_raw: bool = True,
        folder: str | None = None,
        include_retired: bool = False,
        path: str | None = None,
    ) -> dict[str, Any]:
        # Path lookup: fetch one note's full body for graph hops.
        path_norm = (path or "").replace("\\", "/").lstrip("./").strip()
        if path_norm:
            hit = await async_load_memory_hit(
                self._storage, path_norm, include_retired=include_retired
            )
            if hit is None:
                return {
                    "ok": True,
                    "query": query,
                    "path": path_norm,
                    "hits": [],
                    "note": f"memory note not found or not active: {path_norm}",
                }
            try:
                graph = await self.ensure_graph()
                if graph is not None:
                    existing = {str(link.get("path")) for link in hit.get("links") or []}
                    for nbr in await graph.neighbors(path_norm):
                        if nbr in existing or nbr == path_norm:
                            continue
                        hit.setdefault("links", []).append({"path": nbr, "kind": "related"})
                        existing.add(nbr)
            except Exception as exc:
                logger.debug("memory path neighbor enrich skipped: %r", exc)
            return {
                "ok": True,
                "query": query,
                "path": path_norm,
                "hits": [hit],
                "truncated": False,
            }

        skip: tuple[str, ...] = ("raw", "working") if skip_raw else ("working",)
        if folder and folder.strip().lower() == "working":
            skip = ("raw",) if skip_raw else ()
        payload = await async_search_memory_files(
            self._storage,
            query,
            max_hits=max_hits,
            skip_relative_prefixes=skip,
            folder=folder,
            include_retired=include_retired,
        )
        # Soft 1-hop: load neighbor notes directly (no second full-tree scan).
        try:
            graph = await self.ensure_graph()
            if graph is None:
                return payload
            hits = list(payload.get("hits") or [])
            seen = {h.get("path") for h in hits}
            expand: set[str] = set()
            for hit in hits:
                hit_path = str(hit.get("path") or "")
                if not hit_path:
                    continue
                for nbr in await graph.neighbors(hit_path):
                    if nbr not in seen:
                        expand.add(nbr)
            for nbr in sorted(expand):
                if len(hits) >= max_hits:
                    break
                soft = await async_load_memory_hit(
                    self._storage,
                    nbr,
                    include_retired=include_retired,
                    include_body=False,
                    via="graph",
                )
                if soft is None or soft.get("path") in seen:
                    continue
                hits.append(soft)
                seen.add(soft.get("path"))
            payload["hits"] = hits
        except Exception as exc:
            logger.debug("memory graph expand skipped: %r", exc)
        return payload

    async def edit_memory(self, path: str, content: str) -> dict[str, Any]:
        async with self.lock:
            graph = await self.ensure_graph()  # best-effort; mutations work without it
            return await edit_memory_note(
                self._storage, graph, path=path, content=content
            )

    async def update_memory(self, path: str, content: str) -> dict[str, Any]:
        async with self.lock:
            graph = await self.ensure_graph()  # best-effort; mutations work without it
            return await update_memory_note(
                self._storage, graph, path=path, content=content
            )

    async def forget(self, path: str) -> dict[str, Any]:
        async with self.lock:
            graph = await self.ensure_graph()  # best-effort; mutations work without it
            return await forget_memory_note(self._storage, graph, path=path)

    async def promote(self, run_id: str, file: Path) -> None:
        await async_promote_to_memory(run_id, file, self._storage)

    async def gc_processed(self, *, max_age_sec: float = 7 * 24 * 60 * 60) -> dict[str, int]:
        return await self._storage.gc_prefix("raw/processed/", max_age_sec)

    async def _note_updated_at(
        self,
        path: str,
        *,
        graph: MemoryGraphStore | None,
        text: str,
    ) -> float | None:
        """Resolve note age: graph → storage mtime → None."""
        if graph is not None:
            try:
                ts = await graph.get_updated_at(path)
            except Exception as exc:
                logger.debug("gc_working graph get_updated_at skipped %s: %r", path, exc)
                ts = None
            if ts is not None:
                return ts
            meta, _ = parse_memory_note(text)
            if meta is not None:
                await self._on_note_written(path, text)
                try:
                    ts = await graph.get_updated_at(path)
                except Exception:
                    ts = None
                if ts is not None:
                    return ts
        try:
            return await self._storage.mtime(path)
        except Exception as exc:
            logger.debug("gc_working storage mtime failed %s: %r", path, exc)
            return None

    async def gc_working(self, *, ttl_days: float | None = None) -> dict[str, int]:
        """Delete expired working/ notes and drop them from INDEX + graph."""
        ttl = _working_ttl_days() if ttl_days is None else ttl_days
        if ttl <= 0:
            return {"scanned": 0, "deleted": 0, "errors": 0}
        cutoff = time.time() - ttl * 24 * 60 * 60
        scanned = 0
        deleted = 0
        errors = 0
        skipped_no_age = 0
        drop: set[str] = set()
        try:
            paths = await self._storage.list_files("working/")
        except Exception as exc:
            logger.warning("gc_working list failed: %r", exc)
            return {"scanned": 0, "deleted": 0, "errors": 1}
        graph = await self.ensure_graph()
        for rel in paths:
            rel_posix = rel.replace("\\", "/")
            if not rel_posix.startswith("working/") or not rel_posix.endswith(".md"):
                continue
            if rel_posix.count("/") != 1:
                continue
            scanned += 1
            try:
                text = await self._storage.read_text(rel_posix)
                updated_at = await self._note_updated_at(
                    rel_posix, graph=graph, text=text
                )
                if updated_at is None:
                    skipped_no_age += 1
                    continue
                if updated_at >= cutoff:
                    continue
                await self._storage.delete(rel_posix)
                if graph is not None:
                    try:
                        await graph.delete_note(rel_posix)
                    except Exception as exc:
                        logger.debug(
                            "gc_working graph delete skipped %s: %r", rel_posix, exc
                        )
                drop.add(rel_posix)
                deleted += 1
            except Exception as exc:
                logger.warning("gc_working failed for %s: %r", rel_posix, exc)
                errors += 1
        if skipped_no_age:
            logger.warning(
                "gc_working skipped %d note(s) with no age signal "
                "(graph + storage mtime unavailable)",
                skipped_no_age,
            )
        if drop:
            await drop_index_paths(self._storage, drop)
        return {"scanned": scanned, "deleted": deleted, "errors": errors}

    async def export_graph(self, *, refresh: bool = False) -> dict[str, Any]:
        """Return memory-note nodes + wiki/supersedes edges for visualization.

        When ``refresh`` is true (or the sidecar has no nodes yet), rescan note
        files on disk into the graph so Reload in the Mac UI picks up new links.
        """
        graph = await self.ensure_graph()
        if graph is None:
            return {
                "nodes": [],
                "edges": [],
                "note": "memory graph requires local:// storage",
            }
        if refresh:
            await self.rebuild_graph()
            return await graph.export_graph()
        payload = await graph.export_graph()
        if payload.get("nodes"):
            return payload
        # Empty sidecar (pre-graph notes on disk) — backfill once for the viz.
        await self.rebuild_graph()
        return await graph.export_graph()

    async def flush(self) -> None:
        await self._hook.flush()

    async def close(self) -> None:
        if self._graph is not None and self._graph_opened:
            await self._graph.close()
            self._graph_opened = False


__all__ = ["MemorySubsystem"]
