"""EmbeddingProvider protocol + shared vector helpers."""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Cloud embedding adapter used by the knowledge indexer + ANN fusion."""

    @property
    def model_id(self) -> str: ...

    @property
    def dim(self) -> int: ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed passage / document texts (asymmetric retrieval document side)."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """Embed a retrieval query (asymmetric retrieval query side)."""
        ...


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 0.0:
        return vec
    return [x / norm for x in vec]


def maybe_truncate_and_renorm(vec: list[float], dim: int) -> list[float]:
    """Matryoshka-style prefix truncate (or pad) + L2 renorm to ``dim``."""
    if len(vec) == dim:
        return vec
    if len(vec) > dim:
        return l2_normalize(vec[:dim])
    return l2_normalize(vec + [0.0] * (dim - len(vec)))


__all__ = [
    "EmbeddingProvider",
    "l2_normalize",
    "maybe_truncate_and_renorm",
]
