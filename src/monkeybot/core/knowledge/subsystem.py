"""Public KnowledgeSubsystem — indexer + recall for the unified knowledge layer."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal

from monkeybot.core.hooks import HookManager
from monkeybot.core.knowledge.embeddings import create_embedding_provider
from monkeybot.core.knowledge.embeddings.base import EmbeddingProvider
from monkeybot.core.knowledge.fusion import recall as fusion_recall
from monkeybot.core.knowledge.evidence_guard import EvidencePathGuard
from monkeybot.core.knowledge.hook import KnowledgeHook
from monkeybot.core.knowledge.salience import IndexAnnouncer, SearchUsageNudge
from monkeybot.core.knowledge.indexer import KnowledgeIndexer
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex
from monkeybot.core.knowledge.tool import serialize_recall_result
from monkeybot.core.knowledge.types import KnowledgeSettings, RecallHit
from monkeybot.core.persistence.vector_backends import VectorStore, create_vector_store
from monkeybot.core.workspace.protocol import WorkspaceStorage

logger = logging.getLogger(__name__)

SourceFilter = Literal["any", "note", "workspace_file"]


class KnowledgeSubsystem:
    """Owns the local FTS/links index, indexer, and recall API."""

    def __init__(
        self,
        *,
        index: KnowledgeIndex,
        indexer: KnowledgeIndexer,
        settings: KnowledgeSettings,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        self._index = index
        self._indexer = indexer
        self._settings = settings
        self._embedder = embedding_provider
        self._vectors = vector_store
        self._hook = KnowledgeHook(indexer)
        self._evidence_guard = EvidencePathGuard()
        self._announcer = IndexAnnouncer(
            index, embeddings_enabled=self.embeddings_enabled
        )
        self._usage_nudge = SearchUsageNudge()

    @classmethod
    async def create(
        cls,
        *,
        workspace_root: Path,
        settings: KnowledgeSettings,
        knowledge_root: Path | None = None,
        memory_storage: WorkspaceStorage | None = None,
        memory_root: Path | None = None,
        index_path: Path | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None,
    ) -> KnowledgeSubsystem:
        """Open the sidecar DB and construct subsystem (does not run startup scan)."""
        root = Path(knowledge_root) if knowledge_root else Path(settings.knowledge_root)
        db_path = Path(index_path) if index_path else Path(settings.index_path)
        index = KnowledgeIndex(db_path)
        await index.open()

        embedder = embedding_provider
        vectors = vector_store
        if settings.embeddings.enabled:
            if embedder is None:
                embedder = create_embedding_provider(settings.embeddings)
            if vectors is None and embedder is not None:
                try:
                    vectors = create_vector_store(
                        {"type": settings.store.type, "path": settings.store.path}
                    )
                except Exception as exc:
                    logger.warning(
                        "knowledge vector store setup failed; semantic stage off: %r",
                        exc,
                    )
                    vectors = None
            if vectors is not None:
                await vectors.open()
            if embedder is None or vectors is None:
                # Fail soft — keyword FTS + graph still work without embeddings
                embedder = None
                if vectors is not None:
                    await vectors.close()
                vectors = None
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
            memory_storage=memory_storage,
            memory_root=memory_root,
            embedding_provider=embedder,
            vector_store=vectors,
        )
        return cls(
            index=index,
            indexer=indexer,
            settings=settings,
            embedding_provider=embedder,
            vector_store=vectors,
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

    async def recall(
        self,
        query: str,
        *,
        limit: int | None = None,
        path_prefix: str | None = None,
        source: SourceFilter = "any",
    ) -> dict[str, Any]:
        """Run fused recall and return a JSON-serializable payload."""
        if not self._indexer.ready:
            try:
                await self._indexer.ensure_ready()
            except Exception as exc:
                logger.warning("knowledge ensure_ready during recall: %r", exc)

        # Best-effort flush before query — never block recall on a long rescan (F11).
        # Shield so the 0.5s timeout cannot cancel an in-progress index/embed flush.
        stale = False
        try:
            await asyncio.wait_for(
                asyncio.shield(self._indexer.flush()), timeout=0.5
            )
        except TimeoutError:
            stale = True
            logger.debug("knowledge pre-recall flush timed out; serving possibly stale index")
        except Exception as exc:
            logger.debug("knowledge pre-recall flush: %r", exc)

        lim = limit if limit is not None else self._settings.default_limit
        hits: list[RecallHit] = await fusion_recall(
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
        return serialize_recall_result(query, hits, limit=lim, stale=stale)


__all__ = ["KnowledgeSubsystem"]
