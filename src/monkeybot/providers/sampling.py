"""Unified model sampling parameters (temperature, max output tokens).

All providers resolve sampling once at construction via :func:`resolve_model_sampling`.
:func:`~monkeybot.core.config.settings.get_provider_config` is the primary entry point;
direct provider construction falls back to a pinned ``RuntimeConfig`` or
``model.temperature`` / ``model.max_tokens`` in ``monkeybot.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from monkeybot.core.config.snapshot import RuntimeConfig

DEFAULT_MODEL_TEMPERATURE = 0.7
DEFAULT_MODEL_MAX_TOKENS = 60_000

# Shared "do not think" words. ``false`` covers an unquoted YAML ``off``, which
# YAML 1.1 loads as a boolean. ``normalize_reasoning_effort`` maps all of them
# to ``REASONING_EFFORT_NONE`` so providers only check that one value.
REASONING_EFFORT_OFF = frozenset({"off", "none", "disabled", "false"})
REASONING_EFFORT_NONE = "none"
# An unquoted YAML ``on`` / ``yes`` loads as True. That asks for thinking, not a
# level, so it means the provider default.
_REASONING_EFFORT_DEFAULT = frozenset({"true"})


@dataclass(frozen=True, slots=True)
class ModelSamplingConfig:
    """Resolved sampling knobs for a single provider instance."""

    temperature: float
    max_tokens: int
    # Provider-neutral effort string (``low``, ``high``, ``max``, …). None = omit.
    reasoning_effort: str | None = None


def normalize_reasoning_effort(value: object) -> str | None:
    """Lowercase and trim. Empty becomes None so the provider field is omitted.

    Off words (and a YAML boolean ``False``) become ``none``. A YAML ``True``
    becomes None (provider default).
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in REASONING_EFFORT_OFF:
        return REASONING_EFFORT_NONE
    if text in _REASONING_EFFORT_DEFAULT:
        return None
    return text or None


def resolve_model_sampling(
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    config: RuntimeConfig | None = None,
) -> ModelSamplingConfig:
    """Resolve temperature, max output tokens, and reasoning effort.

    ``reasoning_effort=None`` reads ``MODEL_REASONING_EFFORT`` (YAML-only).
    An explicit string, including ``""``, is used as-is and does not re-read
    the environment — ``get_provider_config`` passes ``""`` when the snapshot
    has no effort so a leftover process env var cannot leak back in.
    """
    from monkeybot.core.config.snapshot import env_value_or_current

    resolved_temperature = (
        float(temperature)
        if temperature is not None
        else float(
            env_value_or_current(config, "MODEL_TEMPERATURE", str(DEFAULT_MODEL_TEMPERATURE))
        )
    )
    resolved_max_tokens = (
        int(max_tokens)
        if max_tokens is not None
        else int(env_value_or_current(config, "MODEL_MAX_TOKENS", str(DEFAULT_MODEL_MAX_TOKENS)))
    )
    if reasoning_effort is None:
        raw_effort = env_value_or_current(config, "MODEL_REASONING_EFFORT", "")
    else:
        raw_effort = reasoning_effort
    return ModelSamplingConfig(
        temperature=resolved_temperature,
        max_tokens=resolved_max_tokens,
        reasoning_effort=normalize_reasoning_effort(raw_effort),
    )
