"""Public KnowledgeSubsystem — indexer + search for the unified knowledge layer."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Literal

from monkeybot.core.hooks import HookManager
from monkeybot.core.knowledge.embeddings.base import EmbeddingProvider
from monkeybot.core.knowledge.embeddings.factory import create_embedding_provider
from monkeybot.core.knowledge.evidence_guard import EvidencePathGuard
from monkeybot.core.knowledge.fusion import search as fusion_search
from monkeybot.core.knowledge.hook import KnowledgeHook
from monkeybot.core.knowledge.indexer import KnowledgeIndexer
from monkeybot.core.knowledge.salience import IndexAnnouncer, SearchUsageNudge
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex
from monkeybot.core.knowledge.tool import serialize_search_result
from monkeybot.core.knowledge.types import EmbeddingSettings, KnowledgeSettings, RecallHit
from monkeybot.core.persistence.sqlite_vector import SQLiteVectorStore

logger = logging.getLogger(__name__)

SourceFilter = Literal["any", "note", "workspace_file"]


async def _open_embedding_provider(
    settings: EmbeddingSettings,
    *,
    injected: EmbeddingProvider | None,
) -> tuple[EmbeddingProvider | None, str | None]:
    """Return ``(provider, degraded_reason)``. Soft-degrades on setup failure."""
    if injected is not None:
        return injected, None
    try:
        return create_embedding_provider(settings), None
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else f"provider setup failed ({exc})"
        logger.warning(
            "knowledge embedding provider setup failed; semantic stage off: %s",
            reason,
        )
        return None, reason


async def _open_vector_store(
    settings: KnowledgeSettings,
    *,
    injected: SQLiteVectorStore | None,
    read_only: bool,
) -> tuple[SQLiteVectorStore | None, str | None]:
    """Return ``(store, degraded_reason)``. Soft-degrades on setup failure."""
    if injected is not None:
        await injected.open(read_only=read_only)
        return injected, None

    store_type = (settings.store.type or "sqlite").strip().lower()
    if store_type != "sqlite":
        reason = f"store.type {store_type!r} is not supported"
        logger.warning("knowledge store.type %r unsupported; semantic stage off", store_type)
        return None, reason

    try:
        vectors = SQLiteVectorStore(settings.store.path)
        await vectors.open(read_only=read_only)
        return vectors, None
    except FileNotFoundError as exc:
        if read_only:
            # RO clients: missing vectors.sqlite means ANN is off;
            # keyword search still works against the FTS index.
            logger.info(
                "knowledge vector store absent for read-only client; ANN off: %s",
                settings.store.path,
            )
            return None, f"vector store missing for read-only open ({exc})"
        reason = f"vector store setup failed ({exc})"
        logger.warning(
            "knowledge vector store setup failed; semantic stage off: %r", exc
        )
        return None, reason
    except Exception as exc:
        reason = f"vector store setup failed ({exc})"
        logger.warning(
            "knowledge vector store setup failed; semantic stage off: %r", exc
        )
        return None, reason


class KnowledgeSubsystem:
    """Owns the local FTS/links index, indexer, and search API.

    One gateway process is the sole writer per workspace. Subagents open with
    ``read_only=True`` and may only ``search`` (no indexing / hooks).
    """

    def __init__(
        self,
        *,
        index: KnowledgeIndex,
        indexer: KnowledgeIndexer,
        settings: KnowledgeSettings,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: SQLiteVectorStore | None = None,
        embeddings_degraded_reason: str | None = None,
        read_only: bool = False,
    ) -> None:
        self._index = index
        self._indexer = indexer
        self._settings = settings
        self._embedder = embedding_provider
        self._vectors = vector_store
        self._embeddings_degraded_reason = embeddings_degraded_reason
        self._read_only = read_only
        self._hook = KnowledgeHook(indexer)
        self._evidence_guard = EvidencePathGuard()
        self._announcer = IndexAnnouncer(
            index,
            embeddings_enabled=self.embeddings_enabled,
            embeddings_degraded_reason=embeddings_degraded_reason,
        )
        self._usage_nudge = SearchUsageNudge()

    @classmethod
    async def create(
        cls,
        *,
        workspace_root: Path,
        settings: KnowledgeSettings,
        knowledge_root: Path | None = None,
        index_path: Path | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: SQLiteVectorStore | None = None,
        read_only: bool = False,
    ) -> KnowledgeSubsystem:
        """Open the sidecar DB and construct subsystem (does not run startup scan).

        ``read_only=True`` opens SQLite in ``mode=ro`` for search-only clients
        (subagents). The gateway writer must have created the DB first.
        """
        root = Path(knowledge_root) if knowledge_root else Path(settings.knowledge_root)
        db_path = Path(index_path) if index_path else Path(settings.index_path)
        index = KnowledgeIndex(db_path)
        await index.open(read_only=read_only)

        embedder = embedding_provider
        vectors = vector_store
        # User-facing reason surfaced via IndexAnnouncer when embeddings were
        # requested (knowledge.embeddings.enabled: true) but ended up off —
        # otherwise this is only visible in server logs (see PR #123 review).
        degraded_reason: str | None = None
        if settings.embeddings.enabled:
            embedder, degraded_reason = await _open_embedding_provider(
                settings.embeddings, injected=embedder
            )
            if embedder is not None:
                vectors, store_reason = await _open_vector_store(
                    settings, injected=vectors, read_only=read_only
                )
                if store_reason is not None:
                    degraded_reason = store_reason
            if embedder is None or vectors is None:
                embedder = None
                if vectors is not None:
                    await vectors.close()
                vectors = None
                if degraded_reason is None:
                    degraded_reason = "embedding/vector-store setup did not complete"
            else:
                logger.info(
                    "knowledge embeddings on (provider=%s model=%s store=%s read_only=%s)",
                    settings.embeddings.provider,
                    settings.embeddings.model,
                    settings.store.path,
                    read_only,
                )

        indexer = KnowledgeIndexer(
            index,
            workspace_root=workspace_root,
            knowledge_root=root,
            settings=settings,
            embedding_provider=embedder,
            vector_store=vectors,
        )
        return cls(
            index=index,
            indexer=indexer,
            settings=settings,
            embedding_provider=embedder,
            vector_store=vectors,
            embeddings_degraded_reason=degraded_reason,
            read_only=read_only,
        )

    @property
    def settings(self) -> KnowledgeSettings:
        return self._settings

    @property
    def indexer(self) -> KnowledgeIndexer:
        return self._indexer

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def embeddings_enabled(self) -> bool:
        return self._embedder is not None and self._vectors is not None

    def register_hooks(self, manager: HookManager) -> None:
        if self._read_only:
            logger.debug("knowledge register_hooks skipped (read-only client)")
            return
        self._hook.register(manager)
        self._evidence_guard.register(manager)
        self._announcer.register(manager)
        self._usage_nudge.register(manager)

    async def ensure_ready(self) -> None:
        if self._read_only:
            return
        await self._indexer.ensure_ready()

    async def flush(self) -> None:
        if self._read_only:
            return
        await self._indexer.flush()

    async def close(self) -> None:
        await self._index.close()
        if self._vectors is not None:
            try:
                await self._vectors.close()
            except Exception as exc:
                logger.warning("knowledge vector store close failed: %r", exc)

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        path_prefix: str | None = None,
        source: SourceFilter = "any",
    ) -> dict[str, Any]:
        """Run fused search and return a JSON-serializable payload."""
        stale = False
        if not self._read_only:
            if not self._indexer.ready:
                try:
                    await self._indexer.ensure_ready()
                except Exception as exc:
                    logger.warning("knowledge ensure_ready during search: %r", exc)

            # Best-effort flush — never block search on a long rescan (F11).
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._indexer.flush()), timeout=0.5
                )
            except TimeoutError:
                stale = True
                logger.warning(
                    "knowledge pre-search flush timed out; serving possibly stale index"
                )
            except Exception as exc:
                stale = True
                logger.warning("knowledge pre-search flush failed: %r", exc)

        lim = limit if limit is not None else self._settings.default_limit
        started = time.perf_counter()
        hits: list[RecallHit] = await fusion_search(
            self._index,
            query,
            limit=lim,
            path_prefix=path_prefix,
            source=source,
            embedding_provider=self._embedder,
            vector_store=self._vectors,
            ann_dimensions=self._settings.embeddings.dimensions
            if self._embedder is not None
            else None,
            rrf_k=self._settings.rrf_k,
        )
        payload = serialize_search_result(query, hits, limit=lim, stale=stale)
        logger.info(
            "knowledge search query_len=%d hits=%d stale=%s read_only=%s elapsed_ms=%.1f",
            len((query or "").strip()),
            len(hits),
            stale,
            self._read_only,
            (time.perf_counter() - started) * 1000,
        )
        return payload


__all__ = ["KnowledgeSubsystem"]
