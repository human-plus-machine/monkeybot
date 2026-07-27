"""Verify memory integrity for a monkeybot agent memory directory.

Always runs encoding/unreadable-file repair first (quarantine + INDEX rebuild),
then reports remaining structural issues:

  1. INDEX.md entries that point to missing files (orphaned/stale)
  2. Memory files in typed folders (episodic/semantic/procedural/working) with
     no corresponding INDEX.md entry (unindexed — missed by the organizer)
  3. Episodic files whose processed raw source exists and whose summary is
     suspiciously short (< 20 chars), which often indicates a truncation bug.

Usage:
    uv run scripts/verify_memory.py ~/.monkeybot/agents/default/memory
    # or any other memory root under a local agent
    uv run scripts/verify_memory.py /path/to/any/memory/dir
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from monkeybot.core.memory.integrity import verify_memory_cli
from monkeybot.core.memory.repair import repair_memory_tree
from monkeybot.core.workspace import create_workspace_storage


async def _repair_and_report(memory_root: Path) -> int:
    storage = create_workspace_storage("local://" + str(memory_root.resolve()))
    report = await repair_memory_tree(storage, full_scan=True)
    print(
        "Repair:"
        f"\n  quarantined : {len(report.quarantined)}"
        f"\n  index_rebuilt: {report.index_rebuilt}"
        f"\n  index_pruned : {len(report.index_pruned)}"
        f"\n  entries      : {report.entries_written}"
    )
    if report.quarantined:
        for path in report.quarantined:
            print(f"  [QUARANTINED] {path}")
    if report.index_pruned:
        for path in report.index_pruned:
            print(f"  [PRUNED] {path}")
    print()
    return verify_memory_cli(memory_root)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <memory_dir>")
        sys.exit(1)
    root = Path(sys.argv[1])
    sys.exit(asyncio.run(_repair_and_report(root)))
