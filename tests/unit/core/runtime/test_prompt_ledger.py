"""Prompt-size ledger: estimate from the last provider report, count near a bar."""

from __future__ import annotations

import math

from monkeybot.core.llm.provider import Message
from monkeybot.core.runtime.context_budget import PROMPT_ESTIMATE_CHARS_PER_TOKEN
from monkeybot.core.runtime.loop_usage import reported_prompt_tokens
from monkeybot.core.runtime.prompt_ledger import PromptTokenLedger, request_chars
from monkeybot.core.types.content_blocks import IMAGE_TOKEN_ESTIMATE, Image, Text, ToolResponse
from monkeybot.core.types.types_tools import ToolDef


def _msg(text: str, role: str = "user") -> Message:
    return Message(role=role, content=[Text(text=text)])


def _tool(name: str = "run_command") -> ToolDef:
    return ToolDef(name, "Run", {"type": "object"})


def _accept(
    ledger: PromptTokenLedger, messages: list[Message], tools: list[ToolDef], tokens: int
) -> None:
    decision = ledger.consider(messages, tools, window_tokens=100_000)
    assert decision.needs_count
    ledger.note_real_count(tokens)
    ledger.observe(
        prompt_tokens=tokens,
        output_tokens=10,
        messages=messages,
        tools=tools,
    )


def test_first_call_needs_a_real_count() -> None:
    ledger = PromptTokenLedger()
    decision = ledger.consider([_msg("hi")], [_tool()], window_tokens=100_000)
    assert decision.needs_count
    assert not ledger.trusted


def test_matching_report_trusts_the_anchor_and_a_small_delta_skips_the_count() -> None:
    ledger = PromptTokenLedger()
    tools = [_tool()]
    base = [_msg("hello")]
    _accept(ledger, base, tools, 8_000)
    assert ledger.trusted
    assert ledger.anchor_tokens == 8_000

    grown = [*base, _msg("x" * 30)]
    decision = ledger.consider(grown, tools, window_tokens=100_000)
    assert not decision.needs_count
    assert decision.lo <= decision.tokens <= decision.hi
    assert decision.tokens > 8_000


def test_report_outside_half_to_twice_the_count_is_not_trusted() -> None:
    ledger = PromptTokenLedger()
    messages = [_msg("hi")]
    tools = [_tool()]
    ledger.consider(messages, tools, window_tokens=100_000)
    ledger.note_real_count(1_000)
    ledger.observe(prompt_tokens=3_000, output_tokens=1, messages=messages, tools=tools)
    assert not ledger.trusted
    again = ledger.consider(messages, tools, window_tokens=100_000)
    assert again.needs_count


def test_missing_usage_drops_the_anchor() -> None:
    ledger = PromptTokenLedger()
    messages = [_msg("hi")]
    tools = [_tool()]
    ledger.consider(messages, tools, window_tokens=100_000)
    ledger.note_real_count(1_000)
    ledger.observe(prompt_tokens=0, output_tokens=0, messages=messages, tools=tools)
    assert not ledger.trusted


def test_estimate_outside_its_band_distrusts_the_ledger() -> None:
    ledger = PromptTokenLedger()
    tools = [_tool()]
    base = [_msg("hello")]
    _accept(ledger, base, tools, 8_000)
    grown = [*base, _msg("x" * 30)]
    decision = ledger.consider(grown, tools, window_tokens=100_000)
    assert not decision.needs_count
    ledger.note_estimate(decision)
    ledger.observe(
        prompt_tokens=decision.hi + 1,
        output_tokens=4,
        messages=grown,
        tools=tools,
    )
    assert not ledger.trusted


def test_a_small_tokenizer_disagreement_stays_trusted() -> None:
    """The ratio band of a 30-character edit is a few tokens. Slack covers the rest."""
    ledger = PromptTokenLedger()
    tools = [_tool()]
    base = [_msg("hello")]
    _accept(ledger, base, tools, 8_000)
    grown = [*base, _msg("x" * 30)]
    decision = ledger.consider(grown, tools, window_tokens=100_000)
    assert not decision.needs_count
    assert decision.hi >= decision.tokens + 40
    ledger.note_estimate(decision)
    ledger.observe(
        prompt_tokens=decision.tokens + 40,
        output_tokens=4,
        messages=grown,
        tools=tools,
    )
    assert ledger.trusted


def test_delta_over_a_quarter_of_the_anchor_counts_for_real() -> None:
    ledger = PromptTokenLedger()
    tools = [_tool()]
    base = [_msg("hello")]
    _accept(ledger, base, tools, 1_000)
    huge = [*base, _msg("y" * 10_000)]
    decision = ledger.consider(huge, tools, window_tokens=1_000_000)
    assert decision.needs_count


def test_band_that_contains_a_tier_bar_counts_for_real() -> None:
    ledger = PromptTokenLedger()
    tools = [_tool()]
    base = [_msg("a" * 300)]
    _accept(ledger, base, tools, 68_000)
    grown = [*base, _msg("b" * 5_500)]
    decision = ledger.consider(grown, tools, window_tokens=100_000)
    assert decision.needs_count


def test_shrinking_request_keeps_the_band_ordered() -> None:
    ledger = PromptTokenLedger()
    tools = [_tool()]
    base = [_msg("a" * 900)]
    _accept(ledger, base, tools, 8_000)
    shorter = [_msg("a" * 600)]
    decision = ledger.consider(shorter, tools, window_tokens=100_000)
    assert not decision.needs_count
    assert decision.lo <= decision.tokens <= decision.hi
    assert decision.tokens < 8_000


def test_tool_list_change_forces_a_count_and_drops_the_anchor() -> None:
    ledger = PromptTokenLedger()
    messages = [_msg("hi")]
    _accept(ledger, messages, [_tool("run_command")], 4_000)
    decision = ledger.consider(messages, [_tool("grep")], window_tokens=100_000)
    assert decision.needs_count
    assert ledger.trusted
    ledger.drop_if_tools_changed([_tool("grep")])
    assert not ledger.trusted


def test_post_batch_uses_prompt_plus_output_unless_a_bar_sits_between() -> None:
    ledger = PromptTokenLedger()
    messages = [_msg("hi")]
    tools = [_tool()]
    _accept(ledger, messages, tools, 1_000)
    assert ledger.post_batch_tokens(100_000) == 1_010

    ledger.anchor_output_tokens = 49_000
    assert ledger.post_batch_tokens(100_000) is None


def test_invalidate_and_history_chars_are_stable() -> None:
    ledger = PromptTokenLedger()
    messages = [_msg("hi")]
    tools = [_tool()]
    _accept(ledger, messages, tools, 500)
    assert ledger.anchor_chars == request_chars(messages, tools)
    ledger.invalidate()
    assert ledger.anchor_tokens is None
    assert not ledger.trusted
    assert ledger.post_batch_tokens(100_000) is None


def test_request_chars_counts_an_image_once_whatever_its_bytes() -> None:
    def image_message(n: int) -> Message:
        return Message(
            role="user",
            content=[
                ToolResponse(
                    id="load-1",
                    tool_name="load_file",
                    result=[Image(mime_type="image/png", data="A" * n)],
                )
            ],
        )

    small = request_chars([image_message(1_000)], [])
    large = request_chars([image_message(500_000)], [])
    assert small == large
    assert small == IMAGE_TOKEN_ESTIMATE * 4


def _chars_estimate(messages: list[Message], tools: list[ToolDef]) -> int:
    chars = request_chars(messages, tools)
    if chars <= 0:
        return 0
    return math.ceil(chars / PROMPT_ESTIMATE_CHARS_PER_TOKEN)


def test_fallback_without_an_anchor_uses_three_characters_per_token() -> None:
    ledger = PromptTokenLedger()
    messages = [_msg("abcdef")]
    tools = [_tool()]
    assert ledger.fallback_tokens(messages, tools) == _chars_estimate(messages, tools)


def test_fallback_with_an_anchor_uses_the_point_estimate() -> None:
    ledger = PromptTokenLedger()
    tools = [_tool()]
    base = [_msg("hello")]
    _accept(ledger, base, tools, 8_000)
    grown = [*base, _msg("x" * 30)]
    delta = (request_chars(grown, tools) - ledger.anchor_chars) / PROMPT_ESTIMATE_CHARS_PER_TOKEN
    assert ledger.fallback_tokens(grown, tools) == max(0, int(round(8_000 + delta)))


def test_fallback_with_an_anchor_skips_the_quarter_and_band_checks() -> None:
    ledger = PromptTokenLedger()
    tools = [_tool()]
    base = [_msg("hello")]
    _accept(ledger, base, tools, 1_000)
    huge = [*base, _msg("y" * 10_000)]
    decision = ledger.consider(huge, tools, window_tokens=1_000_000)
    assert decision.needs_count
    delta = (request_chars(huge, tools) - ledger.anchor_chars) / PROMPT_ESTIMATE_CHARS_PER_TOKEN
    assert ledger.fallback_tokens(huge, tools) == max(0, int(round(1_000 + delta)))

    banded = PromptTokenLedger()
    base_band = [_msg("a" * 300)]
    _accept(banded, base_band, tools, 68_000)
    grown = [*base_band, _msg("b" * 5_500)]
    band_decision = banded.consider(grown, tools, window_tokens=100_000)
    assert band_decision.needs_count
    band_delta = (
        request_chars(grown, tools) - banded.anchor_chars
    ) / PROMPT_ESTIMATE_CHARS_PER_TOKEN
    assert banded.fallback_tokens(grown, tools) == max(0, int(round(68_000 + band_delta)))


def test_fallback_ignores_a_changed_tool_list() -> None:
    ledger = PromptTokenLedger()
    messages = [_msg("hi")]
    _accept(ledger, messages, [_tool("run_command")], 4_000)
    other = [_tool("grep")]
    assert ledger.trusted
    assert ledger.fallback_tokens(messages, other) == _chars_estimate(messages, other)


def test_note_fallback_accepts_a_report_within_half_to_twice() -> None:
    ledger = PromptTokenLedger()
    messages = [_msg("hi")]
    tools = [_tool()]
    ledger.consider(messages, tools, window_tokens=100_000)
    ledger.note_fallback(1_000)
    assert not ledger.last_preflight_was_real
    ledger.observe(prompt_tokens=1_500, output_tokens=4, messages=messages, tools=tools)
    assert ledger.trusted
    assert ledger.anchor_tokens == 1_500
    decision = ledger.consider(messages, tools, window_tokens=100_000)
    assert not decision.needs_count


def test_note_fallback_rejects_a_report_outside_half_to_twice() -> None:
    ledger = PromptTokenLedger()
    messages = [_msg("hi")]
    tools = [_tool()]
    ledger.consider(messages, tools, window_tokens=100_000)
    ledger.note_fallback(1_000)
    ledger.observe(prompt_tokens=3_000, output_tokens=1, messages=messages, tools=tools)
    assert not ledger.trusted
    assert not ledger.last_preflight_was_real


def test_note_fallback_is_not_a_real_count() -> None:
    ledger = PromptTokenLedger()
    messages = [_msg("hi")]
    tools = [_tool()]
    ledger.note_fallback(1_000)
    assert not ledger.last_preflight_was_real
    ledger.observe(prompt_tokens=1_000, output_tokens=1, messages=messages, tools=tools)
    assert ledger.trusted


def test_reported_prompt_tokens_do_not_add_gemini_cache_twice() -> None:
    assert (
        reported_prompt_tokens(
            "gemini",
            input_tokens=1_200,
            cache_read_tokens=500,
            cache_creation_tokens=0,
        )
        == 1_200
    )
    assert (
        reported_prompt_tokens(
            "claude",
            input_tokens=100,
            cache_read_tokens=50,
            cache_creation_tokens=20,
        )
        == 170
    )
    assert (
        reported_prompt_tokens(
            "openai",
            input_tokens=100,
            cache_read_tokens=40,
            cache_creation_tokens=0,
        )
        == 140
    )
