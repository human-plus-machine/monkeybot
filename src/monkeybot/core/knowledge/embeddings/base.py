"""EmbeddingProvider protocol, shared batching/retry base, and vector helpers."""

from __future__ import annotations

import asyncio
import logging
import math
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any, Literal, Protocol, runtime_checkable

from monkeybot.core.knowledge.embeddings.sanitize import sanitize_embed_text

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 32
DEFAULT_CONCURRENCY = 4
DEFAULT_REQUEST_TIMEOUT_S = 60.0
RATE_LIMIT_MAX_ATTEMPTS = 3
RATE_LIMIT_BASE_DELAY_S = 1.0

# Retrieval side being embedded; adapters map this to their own API parameter
# (OpenAI-compat ``input_type``, Gemini ``task_type``, prefixes, …).
EmbedKind = Literal["query", "document"]


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


class BaseEmbeddingProvider(ABC):
    """Batching, concurrency, timeout, and rate-limit retry shared by adapters.

    Subclasses implement the single-batch API call (:meth:`_embed_one_batch`)
    and how their SDK reports rate limiting (:meth:`_is_rate_limit_error`).
    """

    def __init__(
        self,
        *,
        model: str,
        dimensions: int,
        provider_label: str,
        batch_size: int = DEFAULT_BATCH_SIZE,
        query_prefix: str = "",
        passage_prefix: str = "",
        concurrency: int = DEFAULT_CONCURRENCY,
        timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        self._model = model.strip()
        if not self._model:
            raise ValueError(
                f"knowledge embeddings model is required (provider={provider_label})"
            )
        self._dim = max(1, int(dimensions))
        self._batch_size = max(1, int(batch_size))
        self._provider_label = provider_label
        self._query_prefix = query_prefix
        self._passage_prefix = passage_prefix
        self._timeout_s = max(1.0, float(timeout_s))
        self._concurrency = max(1, int(concurrency))
        self._sem = asyncio.Semaphore(self._concurrency)

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def batch_size(self) -> int:
        return self._batch_size

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        prepared = [self._passage_prefix + sanitize_embed_text(t or "") for t in texts]
        return await self._embed_batches(prepared, kind="document")

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed_batches(
            [self._query_prefix + sanitize_embed_text(text or "")], kind="query"
        )
        return vectors[0] if vectors else [0.0] * self._dim

    async def _embed_batches(
        self, texts: list[str], *, kind: EmbedKind
    ) -> list[list[float]]:
        batches = [
            texts[i : i + self._batch_size]
            for i in range(0, len(texts), self._batch_size)
        ]
        if not batches:
            return []
        if len(batches) == 1:
            return await self._call_one_batch(batches[0], kind=kind)

        out: list[list[float]] = []
        for start in range(0, len(batches), self._concurrency):
            group = batches[start : start + self._concurrency]
            parts = await asyncio.gather(
                *[self._call_one_batch(batch, kind=kind) for batch in group],
                return_exceptions=True,
            )
            for batch, part in zip(group, parts, strict=True):
                if isinstance(part, TimeoutError):
                    # A stalled endpoint must not be retried per input — that
                    # multiplies the stall by the batch size.
                    raise part
                if isinstance(part, BaseException):
                    logger.warning(
                        "%s embed batch failed (%d inputs): %r; retrying singly",
                        self._provider_label,
                        len(batch),
                        part,
                    )
                    out.extend(await self._embed_batch_singly(batch, kind=kind))
                else:
                    out.extend(part)
        return out

    async def _embed_batch_singly(
        self, texts: list[str], *, kind: EmbedKind
    ) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            try:
                vectors.extend(await self._call_one_batch([text], kind=kind))
            except Exception as exc:
                logger.warning(
                    "%s embed failed one input (model=%s): %r",
                    self._provider_label,
                    self._model,
                    exc,
                )
                raise
        return vectors

    async def _call_one_batch(
        self, texts: list[str], *, kind: EmbedKind
    ) -> list[list[float]]:
        """One batch with concurrency cap, per-request timeout, and backoff."""
        last_exc: BaseException | None = None
        async with self._sem:
            for attempt in range(1, RATE_LIMIT_MAX_ATTEMPTS + 1):
                try:
                    return await asyncio.wait_for(
                        self._embed_one_batch(texts, kind=kind),
                        timeout=self._timeout_s,
                    )
                except TimeoutError:
                    logger.warning(
                        "%s embed timed out after %.0fs (model=%s inputs=%d)",
                        self._provider_label,
                        self._timeout_s,
                        self._model,
                        len(texts),
                    )
                    raise
                except Exception as exc:
                    last_exc = exc
                    if attempt < RATE_LIMIT_MAX_ATTEMPTS and self._is_rate_limit_error(
                        exc
                    ):
                        delay = RATE_LIMIT_BASE_DELAY_S * (2 ** (attempt - 1))
                        logger.warning(
                            "%s embed rate-limited (model=%s attempt=%d/%d); "
                            "retrying in %.1fs: %r",
                            self._provider_label,
                            self._model,
                            attempt,
                            RATE_LIMIT_MAX_ATTEMPTS,
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
        assert last_exc is not None
        raise last_exc

    def _finalize_vector(self, values: Iterable[Any]) -> list[float]:
        """Coerce an API embedding to a unit-length vector of ``self.dim``."""
        vec = [float(x) for x in values]
        if len(vec) == self._dim:
            return l2_normalize(vec)
        return maybe_truncate_and_renorm(vec, self._dim)

    @abstractmethod
    async def _embed_one_batch(
        self, texts: list[str], *, kind: EmbedKind
    ) -> list[list[float]]:
        """Embed one batch via the provider SDK (no retry / timeout here)."""

    @abstractmethod
    def _is_rate_limit_error(self, exc: BaseException) -> bool:
        """True when ``exc`` is a retryable rate-limit response."""


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
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "BaseEmbeddingProvider",
    "EmbedKind",
    "EmbeddingProvider",
    "l2_normalize",
    "maybe_truncate_and_renorm",
]
