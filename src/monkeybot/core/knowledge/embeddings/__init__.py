"""Cloud embedding adapters for the optional semantic knowledge layer."""

from __future__ import annotations

import logging
from typing import Any

from monkeybot.core.knowledge.embeddings.base import EmbeddingProvider
from monkeybot.core.knowledge.embeddings.sanitize import sanitize_embed_text
from monkeybot.core.knowledge.types import EmbeddingSettings

logger = logging.getLogger(__name__)


def create_embedding_provider(settings: EmbeddingSettings) -> EmbeddingProvider | None:
    """Build an embedding provider from settings, or ``None`` when disabled / misconfigured.

    Fail soft: missing key or unknown provider logs a warning and returns ``None``
    so keyword+graph recall still runs.
    """
    if not settings.enabled:
        return None
    provider = (settings.provider or "nvidia").strip().lower()
    try:
        if provider == "nvidia":
            from monkeybot.core.knowledge.embeddings.nvidia import NvidiaEmbeddingProvider

            return NvidiaEmbeddingProvider(
                model=settings.model,
                dimensions=settings.dimensions,
                base_url=settings.base_url,
                batch_size=settings.batch_size,
            )
        logger.warning(
            "knowledge embeddings provider %r not implemented; semantic stage off",
            provider,
        )
        return None
    except Exception as exc:
        logger.warning("knowledge embedding provider setup failed; semantic stage off: %r", exc)
        return None


__all__ = [
    "EmbeddingProvider",
    "create_embedding_provider",
    "sanitize_embed_text",
]
