"""Unified model sampling parameters (temperature, max output tokens).

All providers resolve sampling once at construction via :func:`resolve_model_sampling`.
:func:`~monkeybot.core.config.settings.get_provider_config` is the primary entry point;
direct provider construction falls back to a pinned ``RuntimeConfig`` or
``MODEL_TEMPERATURE`` / ``MODEL_MAX_TOKENS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from monkeybot.core.config.snapshot import RuntimeConfig

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
    config: RuntimeConfig | None = None,
) -> ModelSamplingConfig:
    """Resolve temperature and max output tokens from args, snapshot, or environment."""
    if temperature is not None:
        resolved_temperature = float(temperature)
    else:
        raw = None
        if config is not None:
            raw = config.model.temperature
        if raw is None or not str(raw).strip():
            from monkeybot.core.config.snapshot import env_value

            raw = env_value(config, "MODEL_TEMPERATURE", str(DEFAULT_MODEL_TEMPERATURE))
        resolved_temperature = float(raw)

    if max_tokens is not None:
        resolved_max_tokens = int(max_tokens)
    else:
        raw_max = None
        if config is not None:
            raw_max = config.model.max_tokens
        if raw_max is None or not str(raw_max).strip():
            from monkeybot.core.config.snapshot import env_value

            raw_max = env_value(config, "MODEL_MAX_TOKENS", str(DEFAULT_MODEL_MAX_TOKENS))
        resolved_max_tokens = int(raw_max)

    return ModelSamplingConfig(
        temperature=resolved_temperature,
        max_tokens=resolved_max_tokens,
    )
