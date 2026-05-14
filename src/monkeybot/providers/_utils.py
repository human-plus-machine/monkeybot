"""Shared provider utilities (cost estimation)."""

from __future__ import annotations


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    pricing: dict[str, tuple[float, float]],
) -> float:
    """Return estimated USD cost. ``pricing`` values are (input_$/M, output_$/M)."""
    rates = pricing.get(model, (0.0, 0.0))
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000
