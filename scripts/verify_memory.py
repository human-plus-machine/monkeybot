"""Verify memory integrity for a monkeybot agent memory directory.

Checks three things:
  1. INDEX.md entries that point to missing files (orphaned/stale)
  2. Memory files in typed folders (episodic/semantic/procedural/working) with
     no corresponding INDEX.md entry (unindexed — missed by the organizer)
  3. Episodic files whose processed raw source exists and whose summary is
     suspiciously short (< 20 chars), which often indicates a truncation bug.

Usage:
    uv run scripts/verify_memory.py playground/agent/workspace/data/memory
    uv run scripts/verify_memory.py /path/to/any/memory/dir
"""

from __future__ import annotations

import sys
from pathlib import Path

from monkeybot.core.memory.integrity import verify_memory_cli

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <memory_dir>")
        sys.exit(1)
    root = Path(sys.argv[1])
    sys.exit(verify_memory_cli(root))
