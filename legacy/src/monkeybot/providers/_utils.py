"""Shared provider utilities.

Provides reusable helpers shared across provider implementations,
currently: token-based cost estimation.
"""
from __future__ import annotations


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    pricing: dict[str, tuple[float, float]],
) -> float:
    """Return estimated USD cost. pricing values are (input_$/M, output_$/M).

    Args:
        model: Model name to look up in the pricing table.
        input_tokens: Number of input tokens consumed.
        output_tokens: Number of output tokens generated.
        pricing: Mapping of model name to (input_rate, output_rate) per million tokens.

    Returns:
        Estimated cost in US dollars, or 0.0 if model is not in pricing.
    """
    rates = pricing.get(model, (0.0, 0.0))
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000
