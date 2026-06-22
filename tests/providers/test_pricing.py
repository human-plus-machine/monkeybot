"""Tests for :mod:`monkeybot.providers.pricing`."""

from __future__ import annotations

import pytest

import monkeybot.providers.pricing as pricing
from monkeybot.providers.pricing import estimate_cost, pricing_for


def test_pricing_for_exact_match() -> None:
    assert pricing_for("gpt-5") == (1.25, 10.00, 0.00, 0.125)


def test_pricing_for_longest_prefix_match() -> None:
    result = pricing_for("claude-haiku-4-5@20251101")
    assert result == pricing_for("claude-haiku-4-5")
    assert result == (1.00, 5.00, 1.25, 0.10)


def test_pricing_for_unknown_returns_zeros() -> None:
    assert "gpt-5" in pricing.MODEL_PRICING
    assert pricing_for("nonexistent-model") == (0.0, 0.0, 0.0, 0.0)


def test_estimate_cost_input_output_only() -> None:
    assert estimate_cost("gpt-5", 1_000_000, 1_000_000) == pytest.approx(11.25)


def test_estimate_cost_with_cache_tokens() -> None:
    cost = estimate_cost(
        "claude-sonnet-4",
        0,
        0,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
    )
    assert cost == pytest.approx(4.05)


def test_estimate_cost_unknown_model_zero() -> None:
    assert pricing.MODEL_PRICING.get("gpt-5") is not None
    assert estimate_cost("nope", 1000, 1000, cache_read_tokens=1000) == 0.0


def test_estimate_cost_zero_tokens() -> None:
    assert pricing.MODEL_PRICING.get("gpt-5") is not None
    assert estimate_cost("gpt-5", 0, 0) == 0.0


def test_utils_reexports_estimate_cost() -> None:
    from monkeybot.providers._utils import estimate_cost as ec

    assert ec is pricing.estimate_cost
