"""Model pricing lookup and cost estimation."""

from __future__ import annotations

import logging
from typing import Literal

_log = logging.getLogger(__name__)

# USD per 1M tokens: (input, output, cache_write, cache_read)
# Bedrock/Vertex ids are normalized onto these bare keys (see _normalize).
# Gemini-on-Vertex uses the same gemini-* keys when the model id matches.
# Anthropic cache_write is the 5-minute (1.25× input) rate; estimate_cost
# upgrades writes to 2× input when cache_retention is ``long`` (1h TTL).
MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-4": (3.00, 15.00, 3.75, 0.30),
    "claude-opus-4": (15.00, 75.00, 18.75, 1.50),
    "claude-haiku-4-5": (1.00, 5.00, 1.25, 0.10),
    "gpt-5": (1.25, 10.00, 0.00, 0.125),
    "gemini-2.5-flash": (0.30, 2.50, 0.00, 0.075),
    "gemini-3-flash-preview": (0.30, 2.50, 0.00, 0.075),
}

# Anthropic 1-hour ephemeral cache writes are 2× base input (docs: prompt caching).
_ANTHROPIC_LONG_CACHE_WRITE_MULTIPLIER = 2.0

__all__ = ["MODEL_PRICING", "estimate_cost", "normalize_model_id", "pricing_for"]


def normalize_model_id(model: str) -> str:
    """Strip hosting decorations so Bedrock/Vertex ids match :data:`MODEL_PRICING` keys.

    ``us.anthropic.claude-sonnet-4-6-v1:0`` -> ``claude-sonnet-4-6``.
    """
    name = model.strip()
    # ponytail: string surgery, not a model registry. Add a real alias map only if
    # a provider ships an id whose bare name differs from the Anthropic/OpenAI one.
    name = name.split("/")[-1]  # litellm-style "aws_bedrock/us.anthropic.claude-..."
    name = name.split(":", 1)[0]  # bedrock version suffix ":0"
    for region in ("us.", "eu.", "apac.", "global."):
        if name.startswith(region):
            name = name[len(region) :]
            break
    for vendor in ("anthropic.", "meta.", "amazon.", "google."):
        if name.startswith(vendor):
            name = name[len(vendor) :]
            break
    if name.endswith("-v1"):
        name = name[: -len("-v1")]
    return name


def pricing_for(model: str) -> tuple[float, float, float, float]:
    """Return (input, output, cache_write, cache_read) $/M for ``model``.

    Exact match first, then longest known-prefix match, else zeros.
    Hosted ids (Bedrock, Vertex) are normalized first. Never raises.
    """
    for candidate in (model, normalize_model_id(model)):
        exact = MODEL_PRICING.get(candidate)
        if exact is not None:
            return exact

        best_key: str | None = None
        for key in MODEL_PRICING:
            if candidate.startswith(key) and (best_key is None or len(key) > len(best_key)):
                best_key = key

        if best_key is not None:
            return MODEL_PRICING[best_key]

    _log.warning("unknown model for pricing lookup: %s", model)
    return (0.0, 0.0, 0.0, 0.0)


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_retention: Literal["none", "short", "long"] = "short",
) -> float:
    """Return estimated USD cost using :func:`pricing_for`. Unknown model → 0.0.

    ``cache_retention`` selects the Anthropic cache-write tier when the model
    has a non-zero write rate (Claude family): ``short``/``none`` use the table's
    5-minute 1.25× rate; ``long`` uses 2× input for 1-hour TTL writes. OpenAI /
    Gemini keep ``cache_write=0`` so retention is a no-op for them.
    """
    rin, rout, rcw, rcr = pricing_for(model)
    if cache_retention == "long" and rin > 0 and rcw > 0:
        rcw = rin * _ANTHROPIC_LONG_CACHE_WRITE_MULTIPLIER
    return (
        input_tokens * rin
        + output_tokens * rout
        + cache_creation_tokens * rcw
        + cache_read_tokens * rcr
    ) / 1_000_000
