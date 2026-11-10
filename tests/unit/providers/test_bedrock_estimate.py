"""Bedrock token estimation fallback."""

from __future__ import annotations

from monkeybot.core.types.content_blocks import IMAGE_TOKEN_ESTIMATE
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


def _image_content(data: str) -> dict[str, object]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }


def _estimated(content: list[object]) -> int:
    reset_anthropic_token_estimate_correction()
    return estimate_anthropic_input_tokens(
        system="",
        messages=[{"role": "user", "content": content}],
        tools=None,
    )


def test_image_estimate_is_fixed_at_top_level_and_inside_a_tool_result() -> None:
    text = [{"type": "text", "text": "see"}]
    small = _estimated([*text, _image_content("A" * 100)])
    large = _estimated([*text, _image_content("A" * 500_000)])
    assert small == large
    added = small - _estimated(text)
    assert IMAGE_TOKEN_ESTIMATE <= added < IMAGE_TOKEN_ESTIMATE + 50

    def tool_result(data: str | None) -> list[object]:
        nested: list[object] = [{"type": "text", "text": "ok"}]
        if data is not None:
            nested.append(_image_content(data))
        return [{"type": "tool_result", "tool_use_id": "c1", "content": nested}]

    nested_small = _estimated(tool_result("A" * 100))
    assert nested_small == _estimated(tool_result("A" * 500_000))
    nested_added = nested_small - _estimated(tool_result(None))
    assert IMAGE_TOKEN_ESTIMATE <= nested_added < IMAGE_TOKEN_ESTIMATE + 50


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
