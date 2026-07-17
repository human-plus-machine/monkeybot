"""VectorStore protocol and factory for optional ANN similarity (Config C).

Protocol-only at import time — implementations are lazy-imported in
:func:`create_vector_store` (same pattern as :mod:`backends`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class VectorHit:
    """One ANN similarity hit (chunk-keyed)."""

    chunk_id: str
    path: str
    score: float
    start_line: int | None = None
    end_line: int | None = None


@dataclass(frozen=True)
class VectorChunkRecord:
    """One chunk vector to upsert."""

    chunk_id: str
    path: str
    vector: list[float]
    model_id: str
    dim: int
    start_line: int | None = None
    end_line: int | None = None
    source_type: str = "workspace_file"
    text: str | None = None


@runtime_checkable
class VectorStore(Protocol):
    """Similarity-only store consumed by the knowledge layer."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def upsert(self, chunks: list[VectorChunkRecord]) -> None: ...

    async def delete_by_path(self, path: str) -> None: ...

    async def delete_missing(self, alive_paths: set[str]) -> int: ...

    async def has_path(self, path: str) -> bool: ...

    async def query(
        self,
        vector: list[float],
        *,
        limit: int = 20,
        path_prefix: str | None = None,
        modality: str | None = None,
        dimensions: int | None = None,
    ) -> list[VectorHit]: ...


def create_vector_store(config: dict[str, Any] | None) -> VectorStore | None:
    """Lazy factory. Returns ``None`` when config is missing or type unknown."""
    if not config:
        return None
    store_type = str(config.get("type") or "sqlite").strip().lower()
    if store_type == "sqlite":
        from monkeybot.core.persistence.sqlite_vector import SQLiteVectorStore

        path = config.get("path")
        if not path:
            return None
        return SQLiteVectorStore(str(path))
    # Phase 3+: pgvector, pinecone, …
    raise ValueError(
        f"Unsupported knowledge.store.type {store_type!r}. "
        "Supported in Phase 2: 'sqlite'."
    )


__all__ = [
    "VectorChunkRecord",
    "VectorHit",
    "VectorStore",
    "create_vector_store",
]
