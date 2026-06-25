"""Spill-file inventory notes for large tool outputs."""

from __future__ import annotations

from monkeybot.core.runtime.context_budget import diff_inventory_lines

_INVENTORY_PREFIX = "[Spill inventory —"


def spill_inventory_note(text: str, rel_spill_path: str) -> str:
    """Build an inventory note appended to large tool results."""
    total_chars = len(text)
    total_lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    parts = [
        f"{_INVENTORY_PREFIX} {total_chars} total chars, {total_lines} total lines.",
    ]
    paths = diff_inventory_lines(text)
    if paths:
        parts.append(f"Changed files ({len(paths)}): " + ", ".join(paths[:80]))
        if len(paths) > 80:
            parts.append(f"... and {len(paths) - 80} more paths")
    parts.append(
        f"Full output at: {rel_spill_path} — use read_file with offset/limit to page through it.]"
    )
    return "\n".join(parts)


def spill_min_chars_from_env() -> int:
    import os

    raw = os.environ.get("MONKEYBOT_SPILL_MIN_CHARS", "8000").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 8000
