"""Bedrock token estimation fallback."""

from __future__ import annotations

from monkeybot.providers._utils import (
    estimate_anthropic_input_tokens,
    note_anthropic_token_estimate_observation,
    reset_anthropic_token_estimate_correction,
)


def test_estimate_anthropic_input_tokens_positive() -> None:
    reset_anthropic_token_estimate_correction()
    n = estimate_anthropic_input_tokens(
        system="hello",
        messages=[{"role": "user", "content": "world"}],
        tools=None,
    )
    assert n >= 1


def test_estimate_uses_conservative_chars_per_token() -> None:
    reset_anthropic_token_estimate_correction()
    text = "x" * 300
    n = estimate_anthropic_input_tokens(
        system=text,
        messages=[],
        tools=None,
    )
    # 300 chars / 3 = 100 (not / 4 = 75)
    assert n == 100


def test_estimate_correction_feedback_raises_undercount() -> None:
    reset_anthropic_token_estimate_correction()
    base = estimate_anthropic_input_tokens(
        system="hello world",
        messages=[{"role": "user", "content": "tool result json " * 20}],
        tools=None,
    )
    note_anthropic_token_estimate_observation(estimated=base, actual=base * 2)
    adjusted = estimate_anthropic_input_tokens(
        system="hello world",
        messages=[{"role": "user", "content": "tool result json " * 20}],
        tools=None,
    )
    assert adjusted > base
    reset_anthropic_token_estimate_correction()
