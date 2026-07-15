"""Spill-file inventory notes for large tool outputs."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from monkeybot.core.runtime.context_budget import diff_inventory_lines

_INVENTORY_PREFIX = "[Spill inventory —"
_SPILL_DIR_REL = Path(".monkeybot") / "spill"


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


def spill_root(workspace_root: Path) -> Path:
    """Return ``.monkeybot/spill`` under ``workspace_root``."""
    return Path(workspace_root).resolve() / _SPILL_DIR_REL


def session_spill_dirs(workspace_root: Path, session_id: str) -> list[Path]:
    """Spill directories owned by a chat session (parent + nested subagent threads).

    Parent turns write ``.monkeybot/spill/{session_id}/``. Subagent workers write
    ``.monkeybot/spill/subagent:{session_id}:{suffix}/`` so session-end cleanup
    can remove both with one namespace.
    """
    root = spill_root(workspace_root)
    dirs: list[Path] = [root / session_id]
    if root.is_dir():
        dirs.extend(sorted(root.glob(f"subagent:{session_id}:*")))
    return dirs


def _rmtree_quiet(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


async def cleanup_session_spill_files(workspace_root: Path, session_id: str) -> None:
    """Remove session and subagent spill dirs concurrently (off the event loop).

    Spills must survive across user turns within a session so the model can
    ``read_file`` inventory pointers on later turns. Call this on session end
    (gateway DELETE / process teardown), not at turn start.
    """
    targets = [p for p in session_spill_dirs(workspace_root, session_id) if p.exists()]
    if not targets:
        return
    await asyncio.gather(*(asyncio.to_thread(_rmtree_quiet, p) for p in targets))


def spill_min_chars_from_env() -> int:
    import os

    raw = os.environ.get("MONKEYBOT_SPILL_MIN_CHARS", "8000").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 8000
