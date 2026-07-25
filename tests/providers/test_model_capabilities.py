"""model_capabilities.supports_param: prefix lookup, defaults, precedence."""

from __future__ import annotations

from monkeybot.providers.model_capabilities import supports_param


def test_unknown_model_supports_everything() -> None:
    assert supports_param("claude-3-5-sonnet-20241022", "temperature") is True
    assert supports_param("claude-3-5-sonnet-20241022", "thinking_budget_tokens") is True


def test_known_blocked_model_rejects_temperature() -> None:
    assert supports_param("claude-sonnet-5", "temperature") is False
    assert supports_param("claude-sonnet-5-20260101", "temperature") is False


def test_known_blocked_model_rejects_manual_thinking() -> None:
    assert supports_param("claude-opus-4-7-20260101", "thinking_budget_tokens") is False


def test_known_blocked_model_still_unaffected_for_other_params() -> None:
    assert supports_param("claude-sonnet-5", "some_future_param") is True


def test_longest_prefix_wins() -> None:
    assert supports_param("claude-opus-4-7", "temperature") is False
    assert supports_param("claude-opus-4", "temperature") is True
