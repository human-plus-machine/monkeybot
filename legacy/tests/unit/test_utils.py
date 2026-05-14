"""Unit tests for monkeybot.providers._utils."""
from __future__ import annotations

import pytest

from monkeybot.providers._utils import estimate_cost

_PRICING: dict[str, tuple[float, float]] = {
    "model-a": (1.00, 2.00),
    "model-b": (3.00, 6.00),
}


def test_known_model_returns_nonzero() -> None:
    """Known model with nonzero tokens produces a positive cost."""
    cost = estimate_cost("model-a", input_tokens=1_000_000, output_tokens=0, pricing=_PRICING)
    assert cost == pytest.approx(1.00)


def test_unknown_model_returns_zero() -> None:
    """Model not in pricing table returns 0.0."""
    cost = estimate_cost(
        "unknown-model", input_tokens=500_000, output_tokens=500_000, pricing=_PRICING
    )
    assert cost == 0.0


def test_zero_tokens_returns_zero() -> None:
    """Zero input and output tokens always yields 0.0."""
    cost = estimate_cost("model-a", input_tokens=0, output_tokens=0, pricing=_PRICING)
    assert cost == 0.0


def test_both_token_types_contribute() -> None:
    """Input and output tokens both contribute to total cost."""
    # model-a: input=1$/M, output=2$/M
    # 1M input + 1M output => 1.00 + 2.00 = 3.00
    cost = estimate_cost(
        "model-a", input_tokens=1_000_000, output_tokens=1_000_000, pricing=_PRICING
    )
    assert cost == pytest.approx(3.00)
