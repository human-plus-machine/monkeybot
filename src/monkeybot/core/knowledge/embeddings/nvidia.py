"""NVIDIA cloud embeddings via the OpenAI-compatible ``/v1/embeddings`` API.

Uses ``NVIDIA_API_KEY`` (same key as chat). Default model:
``nvidia/nemotron-3-embed-1b`` (Nemotron-3-Embed-1B, native dim 2048).

The integrate API only returns 2048-d vectors for this model. Smaller configured
dims (default 1024) use Matryoshka-style client-side prefix slice + L2 renorm.

Asymmetric retrieval: prefixes ``query: `` / ``passage: `` per the model card.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Any

from monkeybot.core.knowledge.embeddings.sanitize import sanitize_embed_text

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
_DEFAULT_MODEL = "nvidia/nemotron-3-embed-1b"
# Native Matryoshka width from the model card. The NVIDIA integrate API only
# returns this size; smaller configured dims are sliced client-side.
_NATIVE_DIM = 2048
_DEFAULT_DIM = 1024  # query/index default (Matryoshka prefix)
_DEFAULT_BATCH = 32
# Match chat provider free-tier throughput discipline.
_CONCURRENCY = 4

_QUERY_PREFIX = "query: "
_PASSAGE_PREFIX = "passage: "


class NvidiaEmbeddingProvider:
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
        key = (api_key if api_key is not None else os.environ.get("NVIDIA_API_KEY", "")).strip()
        if not key:
            raise ValueError(
                "NVIDIA_API_KEY is not set. Get a free key at https://build.nvidia.com "
                "and add it to your .env."
            )
        self._api_key = key
        self._base_url = (
            base_url
            or os.environ.get("NVIDIA_BASE_URL")
            or _DEFAULT_BASE_URL
        ).rstrip("/")
        self._model = model.strip() or _DEFAULT_MODEL
        self._dim = max(1, int(dimensions))
        self._batch_size = max(1, int(batch_size))
        self._sem = asyncio.Semaphore(_CONCURRENCY)
        self._client: Any = None

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise ImportError(
                    "NVIDIA embeddings require the openai package. "
                    "Install with: uv sync --extra nvidia"
                ) from exc
            self._client = AsyncOpenAI(base_url=self._base_url, api_key=self._api_key)
        return self._client

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        prefixed = [
            _PASSAGE_PREFIX + sanitize_embed_text(t or "") for t in texts
        ]
        return await self._embed_batches(prefixed)

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed_batches(
            [_QUERY_PREFIX + sanitize_embed_text(text or "")]
        )
        return vectors[0] if vectors else [0.0] * self._dim

    async def _embed_batches(self, texts: list[str]) -> list[list[float]]:
        batches = [
            texts[i : i + self._batch_size]
            for i in range(0, len(texts), self._batch_size)
        ]
        if not batches:
            return []
        if len(batches) == 1:
            return await self._embed_one_batch(batches[0])
        # Concurrent waves; isolate failures so one bad batch cannot abort the rest.
        out: list[list[float]] = []
        wave = max(1, _CONCURRENCY)
        for start in range(0, len(batches), wave):
            group = batches[start : start + wave]
            parts = await asyncio.gather(
                *[self._embed_one_batch(batch) for batch in group],
                return_exceptions=True,
            )
            for batch, part in zip(group, parts, strict=True):
                if isinstance(part, BaseException):
                    logger.warning(
                        "nvidia embed batch failed (%d inputs): %r; retrying singly",
                        len(batch),
                        part,
                    )
                    out.extend(await self._embed_batch_singly(batch))
                else:
                    out.extend(part)
        return out

    async def _embed_batch_singly(self, texts: list[str]) -> list[list[float]]:
        """Retry a failed batch one text at a time; re-raise permanent failures.

        Never returns zero vectors — callers would otherwise poison ANN scores.
        """
        vectors: list[list[float]] = []
        for text in texts:
            try:
                vectors.extend(await self._embed_one_batch([text]))
            except Exception as exc:
                logger.warning(
                    "nvidia embed failed one input (model=%s): %r",
                    self._model,
                    exc,
                )
                raise
        return vectors

    async def _embed_one_batch(self, texts: list[str]) -> list[list[float]]:
        client = self._get_client()
        # integrate.api.nvidia.com only accepts dimensions=2048 for
        # nemotron-3-embed-1b. Matryoshka (e.g. 1024) is client-side:
        # slice prefix + L2 renorm after the full vector returns.
        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": texts,
        }

        async with self._sem:
            try:
                resp = await client.embeddings.create(**kwargs)
            except Exception as exc:
                logger.warning("nvidia embed failed (model=%s): %r", self._model, exc)
                raise

        # API may return out of order; sort by index when present.
        data = sorted(resp.data, key=lambda d: getattr(d, "index", 0))
        vectors: list[list[float]] = []
        for item in data:
            vec = [float(x) for x in item.embedding]
            vectors.append(_maybe_truncate_and_renorm(vec, self._dim))
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"nvidia embed returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        return vectors


def _maybe_truncate_and_renorm(vec: list[float], dim: int) -> list[float]:
    if len(vec) == dim:
        return vec
    if len(vec) > dim:
        sliced = vec[:dim]
        return _l2_normalize(sliced)
    # Shorter than requested — pad (should be rare)
    return _l2_normalize(vec + [0.0] * (dim - len(vec)))


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 0.0:
        return vec
    return [x / norm for x in vec]


__all__ = ["NvidiaEmbeddingProvider"]
