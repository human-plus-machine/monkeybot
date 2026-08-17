"""Knowledge embedding adapters (NVIDIA / OpenAI / Voyage / Gemini / …)."""

from __future__ import annotations

from monkeybot.core.knowledge.embeddings.base import EmbeddingProvider
from monkeybot.core.knowledge.embeddings.factory import (
    create_embedding_provider,
    known_providers,
    provider_defaults,
)
from monkeybot.core.knowledge.embeddings.gemini import GeminiEmbeddingProvider
from monkeybot.core.knowledge.embeddings.nvidia import NvidiaEmbeddingProvider
from monkeybot.core.knowledge.embeddings.openai_compat import OpenAICompatEmbeddingProvider
from monkeybot.core.knowledge.embeddings.sanitize import sanitize_embed_text

__all__ = [
    "EmbeddingProvider",
    "GeminiEmbeddingProvider",
    "NvidiaEmbeddingProvider",
    "OpenAICompatEmbeddingProvider",
    "create_embedding_provider",
    "known_providers",
    "provider_defaults",
    "sanitize_embed_text",
]
