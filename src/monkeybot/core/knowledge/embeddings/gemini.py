"""Gemini / Google GenAI embedding adapter."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from monkeybot.core.knowledge.embeddings.base import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_REQUEST_TIMEOUT_S,
    BaseEmbeddingProvider,
    EmbedKind,
)

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "text-embedding-004"
_DEFAULT_DIM = 768

_TASK_TYPES: dict[str, str] = {
    "query": "RETRIEVAL_QUERY",
    "document": "RETRIEVAL_DOCUMENT",
}
_RATE_LIMIT_MARKERS = ("429", "resource exhausted", "rate")


class GeminiEmbeddingProvider(BaseEmbeddingProvider):
    """Google GenAI ``embed_content`` embeddings (``GEMINI_API_KEY``)."""

    def __init__(
        self,
        *,
        model: str = _DEFAULT_MODEL,
        dimensions: int = _DEFAULT_DIM,
        batch_size: int = DEFAULT_BATCH_SIZE,
        api_key: str | None = None,
        query_prefix: str = "",
        passage_prefix: str = "",
        pass_dimensions: bool = True,
        timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
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
        super().__init__(
            model=(model.strip() or _DEFAULT_MODEL).removeprefix("models/"),
            dimensions=dimensions,
            provider_label="gemini",
            batch_size=batch_size,
            query_prefix=query_prefix,
            passage_prefix=passage_prefix,
            timeout_s=timeout_s,
        )
        self._api_key = key
        self._pass_dimensions = pass_dimensions
        self._client: Any = None

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

    def _is_rate_limit_error(self, exc: BaseException) -> bool:
        msg = str(exc).lower()
        return any(marker in msg for marker in _RATE_LIMIT_MARKERS)

    async def _embed_one_batch(
        self, texts: list[str], *, kind: EmbedKind
    ) -> list[list[float]]:
        client = self._get_client()

        def _call() -> list[list[float]]:
            from google.genai import types

            config = types.EmbedContentConfig(
                task_type=_TASK_TYPES[kind],
                output_dimensionality=self._dim if self._pass_dimensions else None,
            )
            embed_fn = getattr(client.models, "embed_content", None)
            if embed_fn is None:
                raise RuntimeError("google-genai Client.models.embed_content missing")

            # The SDK returns a single-embedding shape for a scalar `contents`.
            contents: Any = texts[0] if len(texts) == 1 else texts
            resp = embed_fn(model=self._model, contents=contents, config=config)
            if len(texts) == 1:
                return [self._extract_one(resp)]
            return self._extract_many(resp, expected=len(texts))

        return await asyncio.to_thread(_call)

    def _extract_one(self, resp: Any) -> list[float]:
        emb = getattr(resp, "embeddings", None) or getattr(resp, "embedding", None)
        if emb is None:
            raise RuntimeError("gemini embed response missing embeddings")
        if isinstance(emb, list):
            if not emb:
                raise RuntimeError("gemini embed response empty embeddings list")
            values = _values_of(emb[0])
        else:
            values = _values_of(emb)
        if values is None:
            raise RuntimeError("gemini embed response missing values")
        return self._finalize_vector(values)

    def _extract_many(self, resp: Any, *, expected: int) -> list[list[float]]:
        emb = getattr(resp, "embeddings", None)
        if not isinstance(emb, list):
            return [self._extract_one(resp)]
        out: list[list[float]] = []
        for item in emb:
            values = _values_of(item)
            if values is None:
                raise RuntimeError("gemini embed item missing values")
            out.append(self._finalize_vector(values))
        if len(out) != expected:
            raise RuntimeError(
                f"gemini embed returned {len(out)} vectors for {expected} inputs"
            )
        return out


def _values_of(item: Any) -> Any:
    values = getattr(item, "values", None)
    if values is None and isinstance(item, (list, tuple)):
        return item
    return values


__all__ = ["GeminiEmbeddingProvider"]
