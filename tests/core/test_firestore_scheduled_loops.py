"""Unit tests for scheduled-loop document mapping."""

from __future__ import annotations

from monkeybot.core.persistence.scheduled_loops import doc_to_scheduled_loop_row


def test_doc_to_scheduled_loop_row_roundtrip_fields() -> None:
    row = doc_to_scheduled_loop_row(
        "demo",
        {
            "session_id": "loop-main",
            "status": "active",
            "prompt": "tick plan",
            "interval_ms": 5000,
            "max_ticks": 3,
            "max_runtime_ms": 3600000,
            "skip_if_busy": 1,
            "tick_index": 2,
            "next_tick_at_ms": 100,
            "started_at_ms": 50,
            "last_tick_at_ms": 90,
            "last_error": None,
            "stop_reason": None,
            "tick_in_flight": 0,
            "worker_id": None,
            "claimed_at_ms": None,
        },
    )
    assert row.loop_id == "demo"
    assert row.session_id == "loop-main"
    assert row.max_ticks == 3
    assert row.skip_if_busy is True
    assert row.tick_index == 2
