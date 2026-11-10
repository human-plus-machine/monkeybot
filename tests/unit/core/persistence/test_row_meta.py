"""Stamp and row-id matching for history.reset."""

from __future__ import annotations

from monkeybot.core.llm.provider import Message
from monkeybot.core.persistence.row_meta import RowMeta, carry_created_at, match_stored
from monkeybot.core.types.content_blocks import Text


def _msg(row_id: str | None) -> Message:
    return Message(role="user", content=[Text(text="x")], row_id=row_id)


def test_match_pairs_duplicate_row_ids_in_stored_order() -> None:
    stored = [
        ("dup", RowMeta(created_at=1, message_id="m1")),
        ("other", RowMeta(created_at=2, message_id="m-other")),
        ("dup", RowMeta(created_at=3, message_id="m2")),
    ]
    matched = match_stored(
        [_msg("dup"), _msg("dup"), _msg("dup"), _msg(None), _msg("missing")],
        stored,
    )
    assert matched == [0, 2, None, None, None]
    assert match_stored([_msg("other"), _msg("dup")], stored) == [1, 0]


def test_match_ignores_blank_stored_ids_and_an_empty_store() -> None:
    stored = [("", RowMeta(created_at=1)), ("keep", RowMeta(created_at=2))]
    assert match_stored([_msg("keep"), _msg("")], stored) == [1, None]
    assert match_stored([_msg("keep")], []) == [None]


def test_carry_keeps_matched_stamps_and_slots_a_new_row_between_them() -> None:
    assert carry_created_at([100, None, 300], now_ms=9_000, step=0) == [100, 100, 300]
    assert carry_created_at([100, None, 300], now_ms=9_000, step=1) == [100, 101, 300]


def test_carry_counts_leading_new_rows_back_from_the_first_known_stamp() -> None:
    assert carry_created_at([None, None, 500], now_ms=9_000, step=1) == [498, 499, 500]
    assert carry_created_at([None, None, 500], now_ms=9_000, step=0) == [500, 500, 500]
    assert carry_created_at([None, 0], now_ms=9_000, step=1) == [-1, 0]


def test_carry_with_no_stored_stamp_ends_at_now() -> None:
    assert carry_created_at([None, None, None], now_ms=1_000, step=0) == [1_000, 1_000, 1_000]
    assert carry_created_at([None, None, None], now_ms=1_000, step=1) == [998, 999, 1_000]
    assert carry_created_at([None], now_ms=1_000, step=1) == [1_000]
    assert carry_created_at([], now_ms=1_000, step=1) == []


def test_carry_bumps_a_stamp_that_would_break_row_order() -> None:
    assert carry_created_at([100, 50, 200], now_ms=9_000, step=0) == [100, 100, 200]
    assert carry_created_at([100, 50, 200], now_ms=9_000, step=1) == [100, 101, 200]
    assert carry_created_at([5, 5, 5], now_ms=9_000, step=0) == [5, 5, 5]


def test_carry_keeps_a_matched_stamp_that_is_ahead_of_now() -> None:
    assert carry_created_at([5_000], now_ms=1, step=0) == [5_000]
    assert carry_created_at([100, None], now_ms=1, step=1) == [100, 101]
