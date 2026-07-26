"""OpenAI-compatible ``/v1/embeddings`` adapter (OpenAI, NVIDIA NIM, Voyage, …)."""

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

_DEFAULT_BATCH = 32
_CONCURRENCY = 4
_RATE_LIMIT_MAX_ATTEMPTS = 3
_RATE_LIMIT_BASE_DELAY_S = 1.0

InputTypeMode = Literal["none", "voyage"]


class OpenAICompatEmbeddingProvider:
    """Hosted embeddings via an OpenAI-compatible ``embeddings.create`` client.

    Prefix convention, Matryoshka handling, and API-key env are configurable so
    NVIDIA / OpenAI / Voyage / custom gateways share one code path.
    """

    def __init__(
        self,
        *,
        model: str,
        dimensions: int,
        base_url: str,
        api_key: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        batch_size: int = _DEFAULT_BATCH,
        query_prefix: str = "",
        passage_prefix: str = "",
        pass_dimensions: bool = True,
        input_type_mode: InputTypeMode = "none",
        install_hint: str = "Install with: uv sync --extra openai",
        provider_label: str = "openai_compat",
    ) -> None:
        key = (
            api_key
            if api_key is not None
            else os.environ.get(api_key_env, "")
        ).strip()
        if not key:
            raise ValueError(
                f"{api_key_env} is not set. Add it to your .env for "
                f"knowledge embeddings (provider={provider_label})."
            )
        self._api_key = key
        self._api_key_env = api_key_env
        self._base_url = base_url.rstrip("/")
        self._model = model.strip()
        if not self._model:
            raise ValueError(
                f"knowledge embeddings model is required (provider={provider_label})"
            )
        self._dim = max(1, int(dimensions))
        self._batch_size = max(1, int(batch_size))
        self._query_prefix = query_prefix
        self._passage_prefix = passage_prefix
        self._pass_dimensions = pass_dimensions
        self._input_type_mode = input_type_mode
        self._install_hint = install_hint
        self._provider_label = provider_label
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
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise ImportError(
                    f"{self._provider_label} embeddings require the openai package. "
                    f"{self._install_hint}"
                ) from exc
            self._client = AsyncOpenAI(base_url=self._base_url, api_key=self._api_key)
        return self._client

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        prepared = [
            self._passage_prefix + sanitize_embed_text(t or "") for t in texts
        ]
        return await self._embed_batches(prepared, input_type="document")

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed_batches(
            [self._query_prefix + sanitize_embed_text(text or "")],
            input_type="query",
        )
        return vectors[0] if vectors else [0.0] * self._dim

    async def _embed_batches(
        self,
        texts: list[str],
        *,
        input_type: Literal["query", "document"],
    ) -> list[list[float]]:
        batches = [
            texts[i : i + self._batch_size]
            for i in range(0, len(texts), self._batch_size)
        ]
        if not batches:
            return []
        if len(batches) == 1:
            return await self._embed_one_batch(batches[0], input_type=input_type)
        out: list[list[float]] = []
        wave = max(1, _CONCURRENCY)
        for start in range(0, len(batches), wave):
            group = batches[start : start + wave]
            parts = await asyncio.gather(
                *[
                    self._embed_one_batch(batch, input_type=input_type)
                    for batch in group
                ],
                return_exceptions=True,
            )
            for batch, part in zip(group, parts, strict=True):
                if isinstance(part, BaseException):
                    logger.warning(
                        "%s embed batch failed (%d inputs): %r; retrying singly",
                        self._provider_label,
                        len(batch),
                        part,
                    )
                    out.extend(
                        await self._embed_batch_singly(batch, input_type=input_type)
                    )
                else:
                    out.extend(part)
        return out

    async def _embed_batch_singly(
        self,
        texts: list[str],
        *,
        input_type: Literal["query", "document"],
    ) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            try:
                vectors.extend(
                    await self._embed_one_batch([text], input_type=input_type)
                )
            except Exception as exc:
                logger.warning(
                    "%s embed failed one input (model=%s): %r",
                    self._provider_label,
                    self._model,
                    exc,
                )
                raise
        return vectors

    async def _embed_one_batch(
        self,
        texts: list[str],
        *,
        input_type: Literal["query", "document"],
    ) -> list[list[float]]:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": texts,
        }
        if self._pass_dimensions:
            kwargs["dimensions"] = self._dim
        if self._input_type_mode == "voyage":
            # Voyage asymmetric retrieval via OpenAI-compat extra_body.
            kwargs["extra_body"] = {"input_type": input_type}

        from monkeybot.providers._openai_compat import is_rate_limit_error

        last_exc: BaseException | None = None
        async with self._sem:
            for attempt in range(1, _RATE_LIMIT_MAX_ATTEMPTS + 1):
                try:
                    resp = await client.embeddings.create(**kwargs)
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt < _RATE_LIMIT_MAX_ATTEMPTS and is_rate_limit_error(exc):
                        delay = _RATE_LIMIT_BASE_DELAY_S * (2 ** (attempt - 1))
                        logger.warning(
                            "%s embed rate-limited (model=%s attempt=%d/%d); "
                            "retrying in %.1fs: %r",
                            self._provider_label,
                            self._model,
                            attempt,
                            _RATE_LIMIT_MAX_ATTEMPTS,
                            delay,
                            exc,
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.warning(
                        "%s embed failed (model=%s): %r",
                        self._provider_label,
                        self._model,
                        exc,
                    )
                    raise
            else:
                assert last_exc is not None
                raise last_exc

        data = sorted(resp.data, key=lambda d: getattr(d, "index", 0))
        vectors: list[list[float]] = []
        for item in data:
            vec = [float(x) for x in item.embedding]
            if self._pass_dimensions:
                # API already returned requested width; still renorm if needed.
                vectors.append(l2_normalize(vec) if len(vec) == self._dim else maybe_truncate_and_renorm(vec, self._dim))
            else:
                vectors.append(maybe_truncate_and_renorm(vec, self._dim))
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"{self._provider_label} embed returned {len(vectors)} vectors "
                f"for {len(texts)} inputs"
            )
        return vectors


__all__ = ["OpenAICompatEmbeddingProvider"]
