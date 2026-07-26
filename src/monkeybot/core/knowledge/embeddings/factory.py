"""Factory + per-provider defaults for knowledge embeddings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from monkeybot.core.knowledge.embeddings.base import EmbeddingProvider
from monkeybot.core.knowledge.embeddings.gemini import GeminiEmbeddingProvider
from monkeybot.core.knowledge.embeddings.nvidia import NvidiaEmbeddingProvider
from monkeybot.core.knowledge.embeddings.openai_compat import OpenAICompatEmbeddingProvider
from monkeybot.core.knowledge.types import EmbeddingSettings


@dataclass(frozen=True)
class ProviderDefaults:
    """Default model / dim / endpoint / auth for one ``embeddings.provider``.

    Every field is forwarded to the adapter by :func:`create_embedding_provider`
    — adapters declare their own fallbacks only for direct construction.
    """

    model: str
    dimensions: int
    base_url: str
    api_key_env: str
    query_prefix: str = ""
    passage_prefix: str = ""
    # False when the API ignores a `dimensions` request and we must Matryoshka
    # slice client-side (NVIDIA integrate returns native width only).
    pass_dimensions: bool = True
    input_type_mode: str = "none"
    install_hint: str = "Install with: uv sync --extra openai"


_PROVIDER_ALIASES: dict[str, str] = {
    "google": "gemini",
}

_PROVIDER_DEFAULTS: dict[str, ProviderDefaults] = {
    "nvidia": ProviderDefaults(
        model="nvidia/nemotron-3-embed-1b",
        dimensions=1024,
        base_url="https://integrate.api.nvidia.com/v1",
        api_key_env="NVIDIA_API_KEY",
        query_prefix="query: ",
        passage_prefix="passage: ",
        pass_dimensions=False,
        install_hint="Install with: uv sync --extra nvidia",
    ),
    "openai": ProviderDefaults(
        model="text-embedding-3-small",
        dimensions=1536,
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        pass_dimensions=True,
        install_hint="Install with: uv sync --extra openai",
    ),
    "openai_compatible": ProviderDefaults(
        model="",  # required in YAML
        dimensions=1536,
        base_url="",  # required in YAML
        api_key_env="OPENAI_API_KEY",
        pass_dimensions=True,
        install_hint="Install with: uv sync --extra openai",
    ),
    "voyage": ProviderDefaults(
        model="voyage-3-lite",
        dimensions=512,
        base_url="https://api.voyageai.com/v1",
        api_key_env="VOYAGE_API_KEY",
        pass_dimensions=True,
        input_type_mode="voyage",
        install_hint="Install with: uv sync --extra openai",
    ),
    "gemini": ProviderDefaults(
        model="text-embedding-004",
        dimensions=768,
        base_url="",  # unused (google-genai SDK)
        api_key_env="GEMINI_API_KEY",
        pass_dimensions=True,
        install_hint="Install with: uv sync --extra gemini",
    ),
}


def normalize_provider_name(provider: str | None) -> str:
    name = (provider or "nvidia").strip().lower() or "nvidia"
    return _PROVIDER_ALIASES.get(name, name)


def provider_defaults(provider: str | None) -> ProviderDefaults:
    name = normalize_provider_name(provider)
    if name not in _PROVIDER_DEFAULTS:
        raise ValueError(f"unknown knowledge embeddings provider: {name!r}")
    return _PROVIDER_DEFAULTS[name]


def known_providers() -> frozenset[str]:
    return frozenset(_PROVIDER_DEFAULTS) | frozenset(_PROVIDER_ALIASES)


def create_embedding_provider(
    settings: EmbeddingSettings,
    *,
    api_key: str | None = None,
) -> EmbeddingProvider:
    """Construct an embedding adapter from resolved ``EmbeddingSettings``."""
    name = normalize_provider_name(settings.provider)
    if name not in _PROVIDER_DEFAULTS:
        raise ValueError(f"knowledge embeddings provider {name!r} is not implemented")

    defaults = _PROVIDER_DEFAULTS[name]
    model = (settings.model or defaults.model).strip()
    dimensions = int(settings.dimensions) if settings.dimensions else defaults.dimensions
    base_url = (settings.base_url or defaults.base_url).strip().rstrip("/")

    if name in {"openai_compatible"} and not base_url:
        raise ValueError(
            "knowledge.embeddings.base_url is required when provider=openai_compatible"
        )
    if name in {"openai_compatible"} and not model:
        raise ValueError(
            "knowledge.embeddings.model is required when provider=openai_compatible"
        )

    if name == "nvidia":
        return NvidiaEmbeddingProvider(
            model=model or defaults.model,
            dimensions=dimensions or defaults.dimensions,
            base_url=base_url or defaults.base_url,
            batch_size=settings.batch_size,
            api_key=api_key,
            query_prefix=defaults.query_prefix,
            passage_prefix=defaults.passage_prefix,
            pass_dimensions=defaults.pass_dimensions,
        )

    if name == "gemini":
        return GeminiEmbeddingProvider(
            model=model or defaults.model,
            dimensions=dimensions or defaults.dimensions,
            batch_size=settings.batch_size,
            api_key=api_key,
            query_prefix=defaults.query_prefix,
            passage_prefix=defaults.passage_prefix,
            pass_dimensions=defaults.pass_dimensions,
        )

    # openai | openai_compatible | voyage — shared OpenAI-compat client
    return OpenAICompatEmbeddingProvider(
        model=model or defaults.model,
        dimensions=dimensions or defaults.dimensions,
        base_url=base_url or defaults.base_url,
        api_key=api_key,
        api_key_env=defaults.api_key_env,
        batch_size=settings.batch_size,
        query_prefix=defaults.query_prefix,
        passage_prefix=defaults.passage_prefix,
        pass_dimensions=defaults.pass_dimensions,
        input_type_mode=defaults.input_type_mode,  # type: ignore[arg-type]
        install_hint=defaults.install_hint,
        provider_label=name,
    )


def apply_provider_defaults(
    *,
    provider: str,
    model: str | None,
    dimensions: int | None,
    base_url: str | None,
    batch_size: int,
) -> dict[str, Any]:
    """Fill missing model/dim/base_url from the provider table (config resolve)."""
    name = normalize_provider_name(provider)
    try:
        defaults = provider_defaults(name)
    except ValueError:
        # Unknown provider — keep caller values; factory will degrade later.
        return {
            "provider": name,
            "model": (model or "").strip() or "unknown",
            "dimensions": dimensions if dimensions and dimensions > 0 else 1024,
            "base_url": (base_url or "").strip().rstrip("/"),
            "batch_size": batch_size,
        }
    return {
        "provider": name,
        "model": (model or "").strip() or defaults.model,
        "dimensions": (
            dimensions if dimensions and dimensions > 0 else defaults.dimensions
        ),
        "base_url": (base_url or "").strip().rstrip("/") or defaults.base_url,
        "batch_size": batch_size,
    }


__all__ = [
    "ProviderDefaults",
    "apply_provider_defaults",
    "create_embedding_provider",
    "known_providers",
    "normalize_provider_name",
    "provider_defaults",
]
