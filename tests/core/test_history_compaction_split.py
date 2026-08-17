"""Token-budgeted head/middle/tail splits for history compaction."""

from __future__ import annotations

from monkeybot.core.llm.provider import Message
from monkeybot.core.runtime.history_compaction import (
    SUMMARY_KEEP_HEAD_COUNT,
    SUMMARY_KEEP_TAIL_MIN,
    SUMMARY_KEEP_TAIL_RATIO,
    _estimate_message_tokens,
    protect_recent_count,
    split_messages_for_compaction,
)
from monkeybot.core.types.content_blocks import File, Image, Text, Thinking, ToolResponse
from monkeybot.core.tools.spill_inventory import spill_budgets_from_window


def _msgs(n: int, *, chars: int = 40) -> list[Message]:
    return [
        Message(
            role="user" if i % 2 == 0 else "assistant",
            content=[Text(text=("x" * chars) + f"-{i}")],
        )
        for i in range(n)
    ]


def test_split_requires_room_for_middle() -> None:
    head, middle, tail = split_messages_for_compaction(
        _msgs(SUMMARY_KEEP_HEAD_COUNT + SUMMARY_KEEP_TAIL_MIN),
        window_tokens=100_000,
    )
    assert middle == []
    assert len(head) + len(tail) == SUMMARY_KEEP_HEAD_COUNT + SUMMARY_KEEP_TAIL_MIN


def test_split_keeps_minimum_tail_on_tiny_window() -> None:
    messages = _msgs(20, chars=400)
    head, middle, tail = split_messages_for_compaction(messages, window_tokens=100)
    assert len(head) == SUMMARY_KEEP_HEAD_COUNT
    assert len(tail) >= SUMMARY_KEEP_TAIL_MIN
    assert middle
    assert head + middle + tail == messages


def test_larger_window_keeps_more_recent_tail() -> None:
    messages = _msgs(40, chars=200)
    _, _, small_tail = split_messages_for_compaction(messages, window_tokens=2_000)
    _, _, large_tail = split_messages_for_compaction(messages, window_tokens=200_000)
    assert len(large_tail) >= len(small_tail)
    # Large window should retain well beyond the old fixed keep of 6.
    assert len(large_tail) > 6


def test_tail_stays_within_ratio_budget_when_possible() -> None:
    messages = _msgs(30, chars=100)
    window = 10_000
    _, _, tail = split_messages_for_compaction(messages, window_tokens=window)
    budget = int(window * SUMMARY_KEEP_TAIL_RATIO)
    # Dropping the oldest kept tail row should not be required to fit — total may
    # slightly exceed when the final added row alone is large, but with uniform
    # small rows we stay at/under budget after the minimum floor.
    used = sum(_estimate_message_tokens(m) for m in tail)
    if len(tail) > SUMMARY_KEEP_TAIL_MIN:
        assert used <= budget + _estimate_message_tokens(tail[0])


def test_protect_recent_count_tracks_tail() -> None:
    messages = _msgs(25, chars=80)
    _, _, tail = split_messages_for_compaction(messages, window_tokens=50_000)
    assert protect_recent_count(messages, window_tokens=50_000) == len(tail)


def test_estimate_ignores_summary_truncation_cap_for_large_tool_results() -> None:
    """Tail sizing must reflect the real payload, not the LLM-summary text cap.

    ``_summary_line_for_message`` truncates tool-result text to a fixed char
    limit for building the summarization prompt. Keep-budget sizing must not
    inherit that cap, or large tool outputs in the tail would be undercounted
    against ``SUMMARY_KEEP_TAIL_RATIO``.
    """
    cap = spill_budgets_from_window(200_000).summary_max_chars
    huge_result_chars = cap * 5
    message = Message(
        role="assistant",
        content=[
            ToolResponse(
                id="call-1",
                tool_name="read_file",
                result=[Text(text="y" * huge_result_chars)],
            )
        ],
    )
    tokens = _estimate_message_tokens(message)
    # A truncation-capped estimate would plateau near cap // 4 tokens; the
    # real estimate must scale with the untruncated payload well beyond that.
    assert tokens > (cap // 4) * 2


def test_estimate_counts_image_thinking_and_file_payloads() -> None:
    """Multimodal / reasoning blocks must not collapse to type-name char counts.

    A type-name fallback (~5–8 chars) would undercount base64 images, long
    thinking traces, and file payloads against ``SUMMARY_KEEP_TAIL_RATIO``.
    """
    image_data = "A" * 40_000
    thinking_text = "r" * 20_000
    file_data = "B" * 30_000
    message = Message(
        role="assistant",
        content=[
            Image(mime_type="image/png", data=image_data),
            Thinking(thinking=thinking_text, signature="sig"),
            File(mime_type="application/pdf", data=file_data),
        ],
    )
    tokens = _estimate_message_tokens(message)
    # Type-name fallback would be ~5–8 tokens total; real payload is >> that.
    min_chars = len(image_data) + len(thinking_text) + len(file_data)
    assert tokens >= min_chars // 4
