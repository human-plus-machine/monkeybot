"""EmbeddingProvider protocol for optional semantic / ANN search."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Cloud (or local) embedder used by the knowledge indexer and recall."""

    @property
    def model_id(self) -> str: ...

    @property
    def dim(self) -> int: ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed passage/document texts (asymmetric encoding)."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """Embed a single retrieval query."""
        ...


__all__ = ["EmbeddingProvider"]
