"""Spill inventory note helpers."""

from __future__ import annotations

from monkeybot.core.runtime.context_budget import diff_inventory_lines
from monkeybot.core.tools.spill_inventory import spill_inventory_note


def test_diff_inventory_lists_changed_paths() -> None:
    diff = "\n".join(
        [
            "diff --git a/src/foo.py b/src/foo.py",
            "+++ b/src/foo.py",
            "diff --git a/src/bar.py b/src/bar.py",
            "+++ b/src/bar.py",
        ]
    )
    paths = diff_inventory_lines(diff)
    assert paths == ["src/foo.py", "src/bar.py"]


def test_spill_inventory_note_includes_counts_and_paths() -> None:
    diff = "diff --git a/a.py b/a.py\n+line\n"
    note = spill_inventory_note(diff, ".monkeybot/spill/t/call.txt")
    assert "total chars" in note
    assert "total lines" in note
    assert "Changed files (1): a.py" in note
    assert ".monkeybot/spill/t/call.txt" in note
