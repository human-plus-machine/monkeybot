"""Verify memory integrity for a monkeybot agent memory directory.

Checks three things:
  1. INDEX.md entries that point to missing files (orphaned/stale)
  2. Memory files in typed folders (episodic/semantic/procedural/working) with
     no corresponding INDEX.md entry (unindexed — missed by the organizer)
  3. Episodic files whose processed raw source exists and whose summary is
     suspiciously short (< 20 chars), which often indicates a truncation bug.

Usage:
    uv run scripts/verify_memory.py playground/agent/data/memory
    uv run scripts/verify_memory.py /path/to/any/memory/dir
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_INDEX_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_TYPED_FOLDERS = {"episodic", "semantic", "procedural", "working"}
_MIN_SUMMARY_CHARS = 20


def _load_index_entries(memory_root: Path) -> list[tuple[str, str]]:
    """Return [(raw_link, full_line), ...] for every [[...]] entry in INDEX.md."""
    index = memory_root / "INDEX.md"
    if not index.exists():
        return []
    entries = []
    for line in index.read_text(encoding="utf-8").splitlines():
        m = _INDEX_LINK_RE.search(line)
        if m:
            entries.append((m.group(1), line.strip()))
    return entries


def _collect_typed_files(memory_root: Path) -> set[str]:
    """Relative paths (e.g. 'episodic/foo.md') for all files in typed folders."""
    found: set[str] = set()
    for folder in _typed_folders_present(memory_root):
        for f in folder.rglob("*.md"):
            if f.is_file():
                found.add(str(f.relative_to(memory_root)))
    return found


def _typed_folders_present(memory_root: Path) -> list[Path]:
    return [memory_root / f for f in _TYPED_FOLDERS if (memory_root / f).is_dir()]


def verify(memory_root: Path) -> int:
    """Run all checks. Returns number of issues found."""
    if not memory_root.exists():
        print(f"ERROR: memory directory not found: {memory_root}")
        return 1

    issues = 0

    index_entries = _load_index_entries(memory_root)
    indexed_paths: set[str] = set()

    # ------------------------------------------------------------------ #
    # Check 1: orphaned INDEX.md entries (link target missing on disk)    #
    # ------------------------------------------------------------------ #
    for link, line in index_entries:
        target = memory_root / link
        indexed_paths.add(link)
        if not target.exists():
            print(f"[ORPHAN]   INDEX.md references missing file: {link}")
            print(f"           entry: {line}")
            issues += 1

    # ------------------------------------------------------------------ #
    # Check 2: typed-folder files not in INDEX.md                        #
    # ------------------------------------------------------------------ #
    all_typed = _collect_typed_files(memory_root)
    for rel in sorted(all_typed):
        if rel not in indexed_paths:
            print(f"[UNINDEXED] file exists but has no INDEX.md entry: {rel}")
            issues += 1

    # ------------------------------------------------------------------ #
    # Check 3: summary suspiciously short (likely organizer truncation)  #
    # ------------------------------------------------------------------ #
    for link, line in index_entries:
        target = memory_root / link
        if not target.exists():
            continue  # already reported above
        text = target.read_text(encoding="utf-8").strip()
        if len(text) < _MIN_SUMMARY_CHARS:
            print(
                f"[SHORT]    summary file may be truncated ({len(text)} chars): {link}"
            )
            print(f"           content: {text!r}")
            issues += 1

    # ------------------------------------------------------------------ #
    # Check 4: processed raw files whose pair in typed folder is absent  #
    # (organizer wrote the raw file but failed to produce the summary)   #
    # ------------------------------------------------------------------ #
    processed_dir = memory_root / "raw" / "processed"
    if processed_dir.exists():
        processed_names = {f.name for f in processed_dir.iterdir() if f.is_file()}
        summarised_names = {Path(p).name for p in all_typed}
        orphaned_raw = processed_names - summarised_names
        for name in sorted(orphaned_raw):
            print(f"[RAW_ONLY] processed raw exists but no summary in typed folder: {name}")
            issues += 1

    # ------------------------------------------------------------------ #
    # Summary                                                             #
    # ------------------------------------------------------------------ #
    total_indexed = len(index_entries)
    total_files = len(all_typed)
    print(
        f"\nMemory root : {memory_root}"
        f"\nINDEX entries : {total_indexed}"
        f"\nTyped files   : {total_files}"
        f"\nIssues found  : {issues}"
    )

    if issues == 0:
        print("All checks passed.")

    return issues


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <memory_dir>")
        sys.exit(1)
    root = Path(sys.argv[1])
    sys.exit(verify(root))
