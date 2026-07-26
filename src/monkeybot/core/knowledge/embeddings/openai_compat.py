"""OpenAI-compatible ``/v1/embeddings`` adapter (OpenAI, NVIDIA NIM, Voyage, …)."""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from monkeybot.core.knowledge.embeddings.base import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_REQUEST_TIMEOUT_S,
    BaseEmbeddingProvider,
    EmbedKind,
)

logger = logging.getLogger(__name__)

InputTypeMode = Literal["none", "voyage"]


class OpenAICompatEmbeddingProvider(BaseEmbeddingProvider):
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
        batch_size: int = DEFAULT_BATCH_SIZE,
        query_prefix: str = "",
        passage_prefix: str = "",
        pass_dimensions: bool = True,
        input_type_mode: InputTypeMode = "none",
        install_hint: str = "Install with: uv sync --extra openai",
        provider_label: str = "openai_compat",
        timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        key = (
            api_key if api_key is not None else os.environ.get(api_key_env, "")
        ).strip()
        if not key:
            raise ValueError(
                f"{api_key_env} is not set. Add it to your .env for "
                f"knowledge embeddings (provider={provider_label})."
            )
        super().__init__(
            model=model,
            dimensions=dimensions,
            provider_label=provider_label,
            batch_size=batch_size,
            query_prefix=query_prefix,
            passage_prefix=passage_prefix,
            timeout_s=timeout_s,
        )
        self._api_key = key
        self._api_key_env = api_key_env
        self._base_url = base_url.rstrip("/")
        self._pass_dimensions = pass_dimensions
        self._input_type_mode = input_type_mode
        self._install_hint = install_hint
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise ImportError(
                    f"{self._provider_label} embeddings require the openai package. "
                    f"{self._install_hint}"
                ) from exc
            self._client = AsyncOpenAI(
                base_url=self._base_url,
                api_key=self._api_key,
                # Cap per-request latency (a slow custom gateway would otherwise
                # stall an indexing wave); retries are handled by the base class.
                timeout=self._timeout_s,
                max_retries=0,
            )
        return self._client

    def _is_rate_limit_error(self, exc: BaseException) -> bool:
        from monkeybot.providers._openai_compat import is_rate_limit_error

        return bool(is_rate_limit_error(exc))

    async def _embed_one_batch(
        self, texts: list[str], *, kind: EmbedKind
    ) -> list[list[float]]:
        client = self._get_client()
        kwargs: dict[str, Any] = {"model": self._model, "input": texts}
        if self._pass_dimensions:
            kwargs["dimensions"] = self._dim
        if self._input_type_mode == "voyage":
            # Voyage asymmetric retrieval via OpenAI-compat extra_body.
            kwargs["extra_body"] = {"input_type": kind}

        resp = await client.embeddings.create(**kwargs)
        data = sorted(resp.data, key=lambda d: getattr(d, "index", 0))
        vectors = [self._finalize_vector(item.embedding) for item in data]
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"{self._provider_label} embed returned {len(vectors)} vectors "
                f"for {len(texts)} inputs"
            )
        return vectors


__all__ = ["OpenAICompatEmbeddingProvider"]
