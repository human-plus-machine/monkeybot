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
