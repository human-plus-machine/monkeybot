"""Character caps for prompt sections."""

from monkeybot.core.prompts.section_budget import (
    SECTION_CHAR_CAPS,
    clip_head_and_tail,
    clip_lines,
)


def test_clip_lines_keeps_a_short_body() -> None:
    assert clip_lines("one\ntwo", 100, note="note") == "one\ntwo"


def test_clip_lines_stops_on_a_whole_line_and_appends_the_note() -> None:
    body = "\n".join(f"line {index:02d}" for index in range(20))
    clipped = clip_lines(body, 40, note="… [truncated]")
    assert len(clipped) <= 40
    assert clipped.endswith("… [truncated]")
    assert "line 00" in clipped
    assert "line 19" not in clipped


def test_clip_lines_cuts_one_oversized_line() -> None:
    clipped = clip_lines("x" * 500, 30, note="… [truncated]")
    assert len(clipped) <= 30
    assert clipped.endswith("… [truncated]")
    assert clipped.startswith("x")


def test_clip_head_and_tail_keeps_both_ends() -> None:
    cap = SECTION_CHAR_CAPS["runtime_notes"]
    text = "HEAD-" + ("m" * (cap + 50)) + "-TAIL"
    clipped = clip_head_and_tail(text, cap)
    assert len(clipped) == cap
    assert clipped.startswith("HEAD-")
    assert clipped.endswith("-TAIL")
    assert "… [runtime notes truncated]" in clipped


def test_clip_head_and_tail_leaves_a_short_note() -> None:
    assert clip_head_and_tail("short note", 8_000) == "short note"
