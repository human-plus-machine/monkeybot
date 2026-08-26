"""Unified model sampling parameters (temperature, max output tokens).

All providers resolve sampling once at construction via :func:`resolve_model_sampling`.
:func:`~monkeybot.core.config.settings.get_provider_config` is the primary entry point;
direct provider construction falls back to ``MODEL_TEMPERATURE`` / ``MODEL_MAX_TOKENS``.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MODEL_TEMPERATURE = 0.7
DEFAULT_MODEL_MAX_TOKENS = 60_000


@dataclass(frozen=True, slots=True)
class ModelSamplingConfig:
    """Resolved sampling knobs for a single provider instance."""

    temperature: float
    max_tokens: int


def resolve_model_sampling(
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> ModelSamplingConfig:
    """Resolve temperature and max output tokens from explicit args or environment."""
    from monkeybot.core.config.snapshot import current_env

    resolved_temperature = (
        float(temperature)
        if temperature is not None
        else float(current_env("MODEL_TEMPERATURE", str(DEFAULT_MODEL_TEMPERATURE)))
    )
    resolved_max_tokens = (
        int(max_tokens)
        if max_tokens is not None
        else int(current_env("MODEL_MAX_TOKENS", str(DEFAULT_MODEL_MAX_TOKENS)))
    )
    return ModelSamplingConfig(
        temperature=resolved_temperature,
        max_tokens=resolved_max_tokens,
    )
