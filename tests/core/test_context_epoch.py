"""Tests for :mod:`monkeybot.core.context.epoch`."""

from __future__ import annotations

from monkeybot.core.context.epoch import ContextEpochTracker, fingerprint_text


def test_fingerprint_stable() -> None:
    assert fingerprint_text("a", "b") == fingerprint_text("a", "b")
    assert fingerprint_text("a", "b") != fingerprint_text("a", "c")


def test_first_reconcile_opens_epoch() -> None:
    tracker = ContextEpochTracker()
    admit = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="\n\n## Skills\n- s1",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
    )
    assert admit.kind == "new_epoch"
    assert admit.epoch_id == 1
    assert admit.leading_system_text == "STABLE\n\n## Skills\n- s1"
    assert admit.mid_conversation_update == ""


def test_unchanged_reuses_leading_baseline() -> None:
    tracker = ContextEpochTracker()
    tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="\n\n## Skills\n- s1",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
    )
    admit = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="\n\n## Skills\n- s1",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
    )
    assert admit.kind == "unchanged"
    assert admit.epoch_id == 1
    assert admit.mid_conversation_update == ""


def test_volatile_update_keeps_leading_baseline() -> None:
    tracker = ContextEpochTracker()
    first = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="\n\n## Skills\n- s1",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
    )
    second = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="\n\n## Skills\n- s2",
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
    )
    assert second.kind == "volatile_updated"
    assert second.epoch_id == first.epoch_id
    assert second.leading_system_text == first.leading_system_text
    assert "## System context update" in second.mid_conversation_update
    assert "- s2" in second.mid_conversation_update
    assert second.changed_sources == ("volatile",)


def test_stable_change_opens_new_epoch() -> None:
    tracker = ContextEpochTracker()
    tracker.reconcile(
        stable_baseline="STABLE1",
        volatile_text="V",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
    )
    admit = tracker.reconcile(
        stable_baseline="STABLE2",
        volatile_text="V",
        stable_fingerprint="s2",
        volatile_fingerprint="v1",
    )
    assert admit.kind == "new_epoch"
    assert admit.epoch_id == 2
    assert admit.leading_system_text.startswith("STABLE2")


def test_changed_sources_names_the_specific_volatile_source() -> None:
    tracker = ContextEpochTracker()
    tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="MEM1SKILLS1",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
        volatile_part_fingerprints={"memory": "mem1", "skills": "sk1"},
    )
    admit = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="MEM2SKILLS1",
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
        volatile_part_fingerprints={"memory": "mem2", "skills": "sk1"},
    )
    assert admit.kind == "volatile_updated"
    assert admit.changed_sources == ("memory",)


def test_changed_sources_names_multiple_sources() -> None:
    tracker = ContextEpochTracker()
    tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="A",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
        volatile_part_fingerprints={"memory": "mem1", "skills": "sk1"},
    )
    admit = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="B",
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
        volatile_part_fingerprints={"memory": "mem2", "skills": "sk2"},
    )
    assert admit.changed_sources == ("memory", "skills")


def test_changed_sources_falls_back_to_volatile_without_part_fingerprints() -> None:
    tracker = ContextEpochTracker()
    tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="A",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
    )
    admit = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="B",
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
    )
    assert admit.changed_sources == ("volatile",)


def test_peek_matches_reconcile_without_mutating_state() -> None:
    """peek() must report the same admit reconcile() would, without advancing state."""
    tracker = ContextEpochTracker()
    tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="\n\n## Skills\n- s1",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
    )

    peeked_first = tracker.peek(
        stable_baseline="STABLE",
        volatile_text="\n\n## Skills\n- s2",
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
    )
    peeked_second = tracker.peek(
        stable_baseline="STABLE",
        volatile_text="\n\n## Skills\n- s2",
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
    )
    # Calling peek() repeatedly must be idempotent (no state mutation).
    assert peeked_first.kind == "volatile_updated"
    assert peeked_first == peeked_second

    # A real reconcile() with the same inputs afterwards must produce an
    # identical admit — proof peek() did not advance state.
    admit = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="\n\n## Skills\n- s2",
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
    )
    assert admit == peeked_first


def test_peek_on_fresh_tracker_opens_epoch_without_mutating() -> None:
    tracker = ContextEpochTracker()
    peeked = tracker.peek(
        stable_baseline="STABLE",
        volatile_text="V",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
    )
    assert peeked.kind == "new_epoch"
    assert peeked.epoch_id == 1

    # State must be untouched: a real reconcile() still opens epoch 1, not 2.
    admit = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="V",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
    )
    assert admit.kind == "new_epoch"
    assert admit.epoch_id == 1


def test_volatile_update_without_content_text_never_reports_cleared() -> None:
    """Without ``volatile_content_text``, emptiness is judged on the full volatile
    text — matching pre-existing behavior for callers that don't separate an
    always-present source (like "current date") from actual content.
    """
    tracker = ContextEpochTracker()
    tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="\n\n## Current date\n2026-07-15\n\n## Skills\n- s1",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
    )
    admit = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="\n\n## Current date\n2026-07-15",
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
    )
    assert admit.kind == "volatile_updated"
    assert "were cleared" not in admit.mid_conversation_update
    assert "## Current date" in admit.mid_conversation_update


def test_volatile_content_text_reports_cleared_despite_always_present_date() -> None:
    """When the caller supplies ``volatile_content_text`` that excludes the
    always-present "current date" section, losing all real content (memory,
    skills, current-request) still produces the "were cleared" message even
    though ``volatile_text`` itself is never empty.
    """
    tracker = ContextEpochTracker()
    tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="\n\n## Current date\n2026-07-15\n\n## Skills\n- s1",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
        volatile_content_text="\n\n## Skills\n- s1",
    )
    admit = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="\n\n## Current date\n2026-07-15",
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
        volatile_content_text="",
    )
    assert admit.kind == "volatile_updated"
    assert "were cleared" in admit.mid_conversation_update


def test_begin_new_epoch_after_compaction() -> None:
    tracker = ContextEpochTracker()
    tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="V1",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
    )
    tracker.begin_new_epoch()
    admit = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="V2",
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
    )
    assert admit.kind == "new_epoch"
    assert admit.epoch_id == 2
    assert admit.leading_system_text == "STABLEV2"
    assert admit.mid_conversation_update == ""
