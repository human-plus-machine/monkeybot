"""Bedrock token estimation fallback."""

from __future__ import annotations

from monkeybot.providers._utils import estimate_anthropic_input_tokens


def test_estimate_anthropic_input_tokens_positive() -> None:
    n = estimate_anthropic_input_tokens(
        system="hello",
        messages=[{"role": "user", "content": "world"}],
        tools=None,
    )
    assert n >= 1
