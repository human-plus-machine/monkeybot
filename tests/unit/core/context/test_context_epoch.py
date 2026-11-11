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
    assert admit.leading_system_text == "STABLE"
    assert admit.context_tail == "## System context update\n\n## Skills\n- s1"


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
    assert admit.leading_system_text == "STABLE"
    assert admit.context_tail == "## System context update\n\n## Skills\n- s1"


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
    assert "## System context update" in second.context_tail
    assert "- s2" in second.context_tail
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


def test_whole_text_tail_keeps_remaining_sections() -> None:
    """A caller that does not pass sections gets the whole new render, not a
    blanket "cleared" line, when some of the volatile text remains.
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
    assert "Cleared:" not in admit.context_tail
    assert "## Current date" in admit.context_tail


def test_an_empty_section_is_left_out_and_the_rest_stay() -> None:
    """A section with no text is omitted. Every section that still has text is
    in the tail, including one that did not change.
    """
    date = "\n\n## Current date\n2026-07-15"
    skills = "\n\n## Skills\n- s1"
    tracker = ContextEpochTracker()
    tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text=date + skills,
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
        volatile_parts={"current_date": date, "skills": skills},
    )
    admit = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text=date,
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
        volatile_parts={"current_date": date, "skills": ""},
    )
    assert admit.kind == "volatile_updated"
    assert admit.changed_sources == ("skills",)
    assert admit.leading_system_text == "STABLE"
    assert "Cleared:" not in admit.context_tail
    assert "replaces the prior" not in admit.context_tail
    assert "## Current date" in admit.context_tail
    assert "## Skills" not in admit.context_tail


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
    assert admit.leading_system_text == "STABLE"
    assert admit.context_tail == "## System context update\n\nV2"


def _parts(todos: str, request: str, memory: str = "\n\n## Memory\nline") -> dict[str, str]:
    return {"memory": memory, "todos": todos, "current_request": request}


def test_tail_is_the_full_render_and_names_only_what_changed() -> None:
    todos_v1 = "\n\n## Todo list\n- t1 old"
    todos_v2 = "\n\n## Todo list\n- t2 new"
    request = "\n\n## Current request\nfix the harness"
    tracker = ContextEpochTracker()
    opened = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text=todos_v1,
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
        volatile_parts=_parts(todos_v1, ""),
    )
    assert opened.kind == "new_epoch"
    assert opened.leading_system_text == "STABLE"
    assert "- t1 old" in opened.context_tail
    assert "line" in opened.context_tail

    changed = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text=todos_v2 + request,
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
        volatile_parts=_parts(todos_v2, request),
    )
    assert changed.kind == "volatile_updated"
    assert changed.changed_sources == ("current_request", "todos")
    assert "replaces the prior" not in changed.context_tail
    assert "- t2 new" in changed.context_tail
    assert "fix the harness" in changed.context_tail
    assert "line" in changed.context_tail
    assert changed.leading_system_text == opened.leading_system_text

    again = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text=todos_v2 + request,
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
        volatile_parts=_parts(todos_v2, request),
    )
    assert again.kind == "unchanged"
    assert again.context_tail == changed.context_tail
    assert again.changed_sources == ()


def test_tail_shows_the_current_text_when_a_section_moves_back() -> None:
    todos_v1 = "\n\n## Todo list\n- t1 old"
    todos_v2 = "\n\n## Todo list\n- t2 new"
    request = "\n\n## Current request\nfix the harness"
    tracker = ContextEpochTracker()
    tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="v1",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
        volatile_parts=_parts(todos_v1, ""),
    )
    tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="v2",
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
        volatile_parts=_parts(todos_v2, request),
    )
    back = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="v3",
        stable_fingerprint="s1",
        volatile_fingerprint="v3",
        volatile_parts=_parts(todos_v1, request),
    )
    assert "- t1 old" in back.context_tail
    assert "- t2 new" not in back.context_tail
    assert "fix the harness" in back.context_tail
    assert "line" in back.context_tail
    assert back.leading_system_text == "STABLE"
    assert back.changed_sources == ("todos",)


def test_new_epoch_keeps_the_live_sections_in_the_tail() -> None:
    todos_v2 = "\n\n## Todo list\n- t2 new"
    tracker = ContextEpochTracker()
    tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="v1",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
        volatile_parts=_parts("\n\n## Todo list\n- t1 old", ""),
    )
    tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="v2",
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
        volatile_parts=_parts(todos_v2, ""),
    )
    tracker.begin_new_epoch()
    fresh = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text=todos_v2,
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
        volatile_parts=_parts(todos_v2, ""),
    )
    assert fresh.kind == "new_epoch"
    assert fresh.leading_system_text == "STABLE"
    assert "- t2 new" in fresh.context_tail
    assert "- t2 new" not in fresh.leading_system_text


def test_peek_tail_matches_reconcile() -> None:
    todos_v2 = "\n\n## Todo list\n- t2 new"
    request = "\n\n## Current request\nfix the harness"
    tracker = ContextEpochTracker()
    tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="v1",
        stable_fingerprint="s1",
        volatile_fingerprint="v1",
        volatile_parts=_parts("\n\n## Todo list\n- t1 old", ""),
    )
    peeked = tracker.peek(
        stable_baseline="STABLE",
        volatile_text="v2",
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
        volatile_parts=_parts(todos_v2, request),
    )
    peeked_again = tracker.peek(
        stable_baseline="STABLE",
        volatile_text="v2",
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
        volatile_parts=_parts(todos_v2, request),
    )
    assert peeked == peeked_again
    assert peeked.context_tail
    admit = tracker.reconcile(
        stable_baseline="STABLE",
        volatile_text="v2",
        stable_fingerprint="s1",
        volatile_fingerprint="v2",
        volatile_parts=_parts(todos_v2, request),
    )
    assert admit == peeked
