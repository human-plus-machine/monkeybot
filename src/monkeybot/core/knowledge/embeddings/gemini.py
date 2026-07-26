"""Gemini / Google GenAI embedding adapter."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Literal

from monkeybot.core.knowledge.embeddings.base import (
    l2_normalize,
    maybe_truncate_and_renorm,
)
from monkeybot.core.knowledge.embeddings.sanitize import sanitize_embed_text

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "text-embedding-004"
_DEFAULT_DIM = 768
_DEFAULT_BATCH = 32
_CONCURRENCY = 4
_RATE_LIMIT_MAX_ATTEMPTS = 3
_RATE_LIMIT_BASE_DELAY_S = 1.0


class GeminiEmbeddingProvider:
    """Google GenAI ``embed_content`` embeddings (``GEMINI_API_KEY``)."""

    def __init__(
        self,
        *,
        model: str = _DEFAULT_MODEL,
        dimensions: int = _DEFAULT_DIM,
        batch_size: int = _DEFAULT_BATCH,
        api_key: str | None = None,
    ) -> None:
        key = (
            api_key
            if api_key is not None
            else (
                os.environ.get("GEMINI_API_KEY", "")
                or os.environ.get("GOOGLE_API_KEY", "")
            )
        ).strip()
        if not key:
            raise ValueError(
                "GEMINI_API_KEY (or GOOGLE_API_KEY) is not set. Add it to your .env "
                "for knowledge embeddings (provider=gemini)."
            )
        self._api_key = key
        self._model = (model.strip() or _DEFAULT_MODEL).removeprefix("models/")
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

    @property
    def batch_size(self) -> int:
        return self._batch_size

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise ImportError(
                    "gemini embeddings require google-genai. "
                    "Install with: uv sync --extra gemini"
                ) from exc
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        cleaned = [sanitize_embed_text(t or "") for t in texts]
        return await self._embed_batches(cleaned, task_type="RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed_batches(
            [sanitize_embed_text(text or "")],
            task_type="RETRIEVAL_QUERY",
        )
        return vectors[0] if vectors else [0.0] * self._dim

    async def _embed_batches(
        self,
        texts: list[str],
        *,
        task_type: Literal["RETRIEVAL_QUERY", "RETRIEVAL_DOCUMENT"],
    ) -> list[list[float]]:
        batches = [
            texts[i : i + self._batch_size]
            for i in range(0, len(texts), self._batch_size)
        ]
        if not batches:
            return []
        out: list[list[float]] = []
        for batch in batches:
            try:
                out.extend(await self._embed_one_batch(batch, task_type=task_type))
            except Exception as exc:
                logger.warning(
                    "gemini embed batch failed (%d inputs): %r; retrying singly",
                    len(batch),
                    exc,
                )
                for text in batch:
                    out.extend(await self._embed_one_batch([text], task_type=task_type))
        return out

    async def _embed_one_batch(
        self,
        texts: list[str],
        *,
        task_type: Literal["RETRIEVAL_QUERY", "RETRIEVAL_DOCUMENT"],
    ) -> list[list[float]]:
        client = self._get_client()

        def _call() -> list[list[float]]:
            from google.genai import types

            config = types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=self._dim,
            )
            embed_fn = getattr(client.models, "embed_content", None)
            if embed_fn is None:
                raise RuntimeError("google-genai Client.models.embed_content missing")

            if len(texts) == 1:
                resp = embed_fn(
                    model=self._model,
                    contents=texts[0],
                    config=config,
                )
                return [_extract_one_embedding(resp, self._dim)]

            resp = embed_fn(
                model=self._model,
                contents=texts,
                config=config,
            )
            return _extract_embeddings(resp, expected=len(texts), dim=self._dim)

        last_exc: BaseException | None = None
        async with self._sem:
            for attempt in range(1, _RATE_LIMIT_MAX_ATTEMPTS + 1):
                try:
                    return await asyncio.to_thread(_call)
                except Exception as exc:
                    last_exc = exc
                    msg = str(exc).lower()
                    rate_limited = "429" in msg or "resource exhausted" in msg or "rate" in msg
                    if attempt < _RATE_LIMIT_MAX_ATTEMPTS and rate_limited:
                        delay = _RATE_LIMIT_BASE_DELAY_S * (2 ** (attempt - 1))
                        logger.warning(
                            "gemini embed rate-limited (model=%s attempt=%d/%d); "
                            "retrying in %.1fs: %r",
                            self._model,
                            attempt,
                            _RATE_LIMIT_MAX_ATTEMPTS,
                            delay,
                            exc,
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.warning("gemini embed failed (model=%s): %r", self._model, exc)
                    raise
        assert last_exc is not None
        raise last_exc


def _extract_one_embedding(resp: Any, dim: int) -> list[float]:
    emb = getattr(resp, "embeddings", None) or getattr(resp, "embedding", None)
    if emb is None:
        raise RuntimeError("gemini embed response missing embeddings")
    if isinstance(emb, list):
        if not emb:
            raise RuntimeError("gemini embed response empty embeddings list")
        values = getattr(emb[0], "values", None)
        if values is None and isinstance(emb[0], (list, tuple)):
            values = emb[0]
    else:
        values = getattr(emb, "values", None)
        if values is None and isinstance(emb, (list, tuple)):
            values = emb
    if values is None:
        raise RuntimeError("gemini embed response missing values")
    vec = [float(x) for x in values]
    return (
        l2_normalize(vec)
        if len(vec) == dim
        else maybe_truncate_and_renorm(vec, dim)
    )


def _extract_embeddings(resp: Any, *, expected: int, dim: int) -> list[list[float]]:
    emb = getattr(resp, "embeddings", None)
    if emb is None:
        # Single-embedding response shape for a list input.
        return [_extract_one_embedding(resp, dim)]
    if not isinstance(emb, list):
        return [_extract_one_embedding(resp, dim)]
    out: list[list[float]] = []
    for item in emb:
        values = getattr(item, "values", None)
        if values is None and isinstance(item, (list, tuple)):
            values = item
        if values is None:
            raise RuntimeError("gemini embed item missing values")
        vec = [float(x) for x in values]
        out.append(
            l2_normalize(vec) if len(vec) == dim else maybe_truncate_and_renorm(vec, dim)
        )
    if len(out) != expected:
        raise RuntimeError(
            f"gemini embed returned {len(out)} vectors for {expected} inputs"
        )
    return out


__all__ = ["GeminiEmbeddingProvider"]
