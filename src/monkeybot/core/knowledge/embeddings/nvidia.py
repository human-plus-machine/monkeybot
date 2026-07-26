"""NVIDIA cloud embeddings via the OpenAI-compatible ``/v1/embeddings`` API.

Uses ``NVIDIA_API_KEY`` (same key as chat). Default model:
``nvidia/nemotron-3-embed-1b`` (Nemotron-3-Embed-1B, native dim 2048).

The integrate API only returns 2048-d vectors for this model. Smaller configured
dims (default 1024) use Matryoshka-style client-side prefix slice + L2 renorm.

Asymmetric retrieval: prefixes ``query: `` / ``passage: `` per the model card.
"""

from __future__ import annotations

import os

from monkeybot.core.knowledge.embeddings.openai_compat import OpenAICompatEmbeddingProvider

_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
_DEFAULT_MODEL = "nvidia/nemotron-3-embed-1b"
_DEFAULT_DIM = 1024
_DEFAULT_BATCH = 32


class NvidiaEmbeddingProvider(OpenAICompatEmbeddingProvider):
    """Hosted Nemotron / NVIDIA NIM embeddings (OpenAI-compat client)."""

    def __init__(
        self,
        *,
        model: str = _DEFAULT_MODEL,
        dimensions: int = _DEFAULT_DIM,
        base_url: str | None = None,
        batch_size: int = _DEFAULT_BATCH,
        api_key: str | None = None,
    ) -> None:
        key = (
            api_key if api_key is not None else os.environ.get("NVIDIA_API_KEY", "")
        ).strip()
        if not key:
            raise ValueError(
                "NVIDIA_API_KEY is not set. Get a free key at https://build.nvidia.com "
                "and add it to your .env."
            )
        resolved_base = (
            base_url
            or os.environ.get("NVIDIA_BASE_URL")
            or _DEFAULT_BASE_URL
        )
        super().__init__(
            model=model.strip() or _DEFAULT_MODEL,
            dimensions=dimensions,
            base_url=resolved_base,
            api_key=key,
            api_key_env="NVIDIA_API_KEY",
            batch_size=batch_size,
            query_prefix="query: ",
            passage_prefix="passage: ",
            # integrate.api.nvidia.com only accepts native 2048 for this model;
            # Matryoshka is client-side truncate + renorm.
            pass_dimensions=False,
            install_hint="Install with: uv sync --extra nvidia",
            provider_label="nvidia",
        )


__all__ = ["NvidiaEmbeddingProvider"]
