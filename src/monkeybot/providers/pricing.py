"""Model pricing lookup and cost estimation."""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

# USD per 1M tokens: (input, output, cache_write, cache_read)
# TODO: add Bedrock model entries — aws_bedrock users get cost_usd 0.0 until then.
# Gemini-on-Vertex uses the same gemini-* keys when the model id matches.
MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-4": (3.00, 15.00, 3.75, 0.30),
    "claude-haiku-4-5": (1.00, 5.00, 1.25, 0.10),
    "gpt-5": (1.25, 10.00, 0.00, 0.125),
    "gemini-2.5-flash": (0.30, 2.50, 0.00, 0.075),
    "gemini-3-flash-preview": (0.30, 2.50, 0.00, 0.075),
}

__all__ = ["MODEL_PRICING", "estimate_cost", "pricing_for"]


def pricing_for(model: str) -> tuple[float, float, float, float]:
    """Return (input, output, cache_write, cache_read) $/M for ``model``.

    Exact match first, then longest known-prefix match, else zeros.
    Never raises on unknown models.
    """
    exact = MODEL_PRICING.get(model)
    if exact is not None:
        return exact

    best_key: str | None = None
    for key in MODEL_PRICING:
        if model.startswith(key) and (best_key is None or len(key) > len(best_key)):
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
) -> float:
    """Return estimated USD cost using :func:`pricing_for`. Unknown model → 0.0."""
    rin, rout, rcw, rcr = pricing_for(model)
    return (
        input_tokens * rin
        + output_tokens * rout
        + cache_creation_tokens * rcw
        + cache_read_tokens * rcr
    ) / 1_000_000
