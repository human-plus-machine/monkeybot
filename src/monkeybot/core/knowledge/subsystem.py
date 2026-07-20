"""Public KnowledgeSubsystem — indexer + search for the unified knowledge layer."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Literal

from monkeybot.core.hooks import HookManager
from monkeybot.core.knowledge.embeddings.nvidia import NvidiaEmbeddingProvider
from monkeybot.core.knowledge.evidence_guard import EvidencePathGuard
from monkeybot.core.knowledge.fusion import search as fusion_search
from monkeybot.core.knowledge.hook import KnowledgeHook
from monkeybot.core.knowledge.indexer import KnowledgeIndexer
from monkeybot.core.knowledge.salience import IndexAnnouncer, SearchUsageNudge
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex
from monkeybot.core.knowledge.tool import serialize_search_result
from monkeybot.core.knowledge.types import KnowledgeSettings, RecallHit
from monkeybot.core.persistence.sqlite_vector import SQLiteVectorStore

logger = logging.getLogger(__name__)

SourceFilter = Literal["any", "note", "workspace_file"]


class KnowledgeSubsystem:
    """Owns the local FTS/links index, indexer, and search API.

    Process-local indexer queues assume a single gateway writer per workspace.
    """

    def __init__(
        self,
        *,
        index: KnowledgeIndex,
        indexer: KnowledgeIndexer,
        settings: KnowledgeSettings,
        embedding_provider: NvidiaEmbeddingProvider | None = None,
        vector_store: SQLiteVectorStore | None = None,
        embeddings_degraded_reason: str | None = None,
    ) -> None:
        self._index = index
        self._indexer = indexer
        self._settings = settings
        self._embedder = embedding_provider
        self._vectors = vector_store
        self._embeddings_degraded_reason = embeddings_degraded_reason
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
        embedding_provider: NvidiaEmbeddingProvider | None = None,
        vector_store: SQLiteVectorStore | None = None,
    ) -> KnowledgeSubsystem:
        """Open the sidecar DB and construct subsystem (does not run startup scan)."""
        root = Path(knowledge_root) if knowledge_root else Path(settings.knowledge_root)
        db_path = Path(index_path) if index_path else Path(settings.index_path)
        index = KnowledgeIndex(db_path)
        await index.open()

        embedder = embedding_provider
        vectors = vector_store
        # User-facing reason surfaced via IndexAnnouncer when embeddings were
        # requested (knowledge.embeddings.enabled: true) but ended up off —
        # otherwise this is only visible in server logs (see PR #123 review).
        degraded_reason: str | None = None
        if settings.embeddings.enabled:
            if embedder is None:
                provider = (settings.embeddings.provider or "nvidia").strip().lower()
                if provider != "nvidia":
                    degraded_reason = f"provider {provider!r} is not implemented"
                    logger.warning(
                        "knowledge embeddings provider %r not implemented; semantic stage off",
                        provider,
                    )
                else:
                    try:
                        embedder = NvidiaEmbeddingProvider(
                            model=settings.embeddings.model,
                            dimensions=settings.embeddings.dimensions,
                            base_url=settings.embeddings.base_url,
                            batch_size=settings.embeddings.batch_size,
                        )
                    except Exception as exc:
                        degraded_reason = f"provider setup failed ({exc})"
                        logger.warning(
                            "knowledge embedding provider setup failed; semantic stage off: %r",
                            exc,
                        )
                        embedder = None
            if vectors is None and embedder is not None:
                store_type = (settings.store.type or "sqlite").strip().lower()
                if store_type != "sqlite":
                    degraded_reason = f"store.type {store_type!r} is not supported"
                    logger.warning(
                        "knowledge store.type %r unsupported; semantic stage off",
                        store_type,
                    )
                else:
                    try:
                        vectors = SQLiteVectorStore(settings.store.path)
                        await vectors.open()
                    except Exception as exc:
                        degraded_reason = f"vector store setup failed ({exc})"
                        logger.warning(
                            "knowledge vector store setup failed; semantic stage off: %r",
                            exc,
                        )
                        vectors = None
            elif vectors is not None:
                await vectors.open()
            if embedder is None or vectors is None:
                embedder = None
                if vectors is not None:
                    await vectors.close()
                vectors = None
                if degraded_reason is None:
                    degraded_reason = "embedding/vector-store setup did not complete"
            else:
                logger.info(
                    "knowledge embeddings on (provider=%s model=%s store=%s)",
                    settings.embeddings.provider,
                    settings.embeddings.model,
                    settings.store.path,
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
        )

    @property
    def settings(self) -> KnowledgeSettings:
        return self._settings

    @property
    def indexer(self) -> KnowledgeIndexer:
        return self._indexer

    @property
    def embeddings_enabled(self) -> bool:
        return self._embedder is not None and self._vectors is not None

    def register_hooks(self, manager: HookManager) -> None:
        self._hook.register(manager)
        self._evidence_guard.register(manager)
        self._announcer.register(manager)
        self._usage_nudge.register(manager)

    async def ensure_ready(self) -> None:
        await self._indexer.ensure_ready()

    async def flush(self) -> None:
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
        if not self._indexer.ready:
            try:
                await self._indexer.ensure_ready()
            except Exception as exc:
                logger.warning("knowledge ensure_ready during search: %r", exc)

        # Best-effort flush — never block search on a long rescan (F11).
        stale = False
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
            "knowledge search query_len=%d hits=%d stale=%s elapsed_ms=%.1f",
            len((query or "").strip()),
            len(hits),
            stale,
            (time.perf_counter() - started) * 1000,
        )
        return payload

__all__ = ["KnowledgeSubsystem"]
