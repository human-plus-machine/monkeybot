"""Cloud embedding adapters for the optional semantic knowledge layer."""

from __future__ import annotations

from monkeybot.core.knowledge.embeddings.nvidia import NvidiaEmbeddingProvider
from monkeybot.core.knowledge.embeddings.sanitize import sanitize_embed_text

__all__ = [
    "NvidiaEmbeddingProvider",
    "sanitize_embed_text",
]
