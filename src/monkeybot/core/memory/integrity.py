"""Structural integrity checks for on-disk markdown memory trees.

Used by ``scripts/verify_memory.py``, pytest evals (T1), and future tooling.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from monkeybot.core.memory.index_format import wiki_target_from_line

_TYPED_FOLDERS = frozenset({"episodic", "semantic", "procedural", "working"})
DEFAULT_MIN_SUMMARY_CHARS = 20


def _load_index_entries(memory_root: Path) -> list[tuple[str, str]]:
    """Return [(raw_link, full_line), ...] for every [[...]] entry in INDEX.md."""
    index = memory_root / "INDEX.md"
    if not index.exists():
        return []
    entries: list[tuple[str, str]] = []
    for line in index.read_text(encoding="utf-8").splitlines():
        target = wiki_target_from_line(line)
        if target:
            entries.append((target, line.strip()))
    return entries


def _typed_folders_present(memory_root: Path) -> list[Path]:
    return [memory_root / f for f in sorted(_TYPED_FOLDERS) if (memory_root / f).is_dir()]


def _collect_typed_files(memory_root: Path) -> set[str]:
    """Relative paths (e.g. 'episodic/foo.md') for all files in typed folders."""
    found: set[str] = set()
    for folder in _typed_folders_present(memory_root):
        for f in folder.rglob("*.md"):
            if f.is_file():
                found.add(str(f.relative_to(memory_root)))
    return found


@dataclass(frozen=True)
class IntegrityResult:
    """Counts from :meth:`MemoryIntegrityChecker.run`."""

    orphan_count: int
    unindexed_count: int
    short_summary_count: int
    raw_only_count: int
    total_index_entries: int
    total_typed_files: int

    @property
    def total_issues(self) -> int:
        return (
            self.orphan_count
            + self.unindexed_count
            + self.short_summary_count
            + self.raw_only_count
        )


class MemoryIntegrityChecker:
    """Verify memory directory structure (INDEX vs typed folders vs raw)."""

    def __init__(self, memory_root: Path, min_summary_chars: int = DEFAULT_MIN_SUMMARY_CHARS) -> None:
        self._memory_root = memory_root
        self._min_summary_chars = min_summary_chars

    def run(self) -> IntegrityResult:
        """Run all checks. Missing ``memory_root`` yields zero issues (no crash)."""
        root = self._memory_root
        if not root.exists():
            return IntegrityResult(0, 0, 0, 0, 0, 0)

        index_entries = _load_index_entries(root)
        indexed_paths: set[str] = set()

        orphan_count = 0
        for link, _line in index_entries:
            target = root / link
            indexed_paths.add(link)
            if not target.exists():
                orphan_count += 1

        all_typed = _collect_typed_files(root)
        unindexed_count = 0
        for rel in sorted(all_typed):
            if rel not in indexed_paths:
                unindexed_count += 1

        short_summary_count = 0
        for link, _line in index_entries:
            target = root / link
            if not target.exists():
                continue
            text = target.read_text(encoding="utf-8").strip()
            if len(text) < self._min_summary_chars:
                short_summary_count += 1

        raw_only_count = 0
        processed_dir = root / "raw" / "processed"
        if processed_dir.exists():
            processed_names = {f.name for f in processed_dir.iterdir() if f.is_file()}
            summarised_names = {Path(p).name for p in all_typed}
            orphaned_raw = processed_names - summarised_names
            raw_only_count = len(orphaned_raw)

        return IntegrityResult(
            orphan_count=orphan_count,
            unindexed_count=unindexed_count,
            short_summary_count=short_summary_count,
            raw_only_count=raw_only_count,
            total_index_entries=len(index_entries),
            total_typed_files=len(all_typed),
        )


def verify_memory_cli(memory_root: Path) -> int:
    """CLI entry: print human-readable report; return exit code (0 = ok).

    If ``memory_root`` does not exist, prints an error and returns ``1``.
    """
    if not memory_root.exists():
        print(f"ERROR: memory directory not found: {memory_root}")
        return 1

    index_entries = _load_index_entries(memory_root)
    indexed_paths: set[str] = set()
    issues = 0

    for link, line in index_entries:
        target = memory_root / link
        indexed_paths.add(link)
        if not target.exists():
            print(f"[ORPHAN]   INDEX.md references missing file: {link}")
            print(f"           entry: {line}")
            issues += 1

    all_typed = _collect_typed_files(memory_root)
    for rel in sorted(all_typed):
        if rel not in indexed_paths:
            print(f"[UNINDEXED] file exists but has no INDEX.md entry: {rel}")
            issues += 1

    min_chars = DEFAULT_MIN_SUMMARY_CHARS
    for link, _line in index_entries:
        target = memory_root / link
        if not target.exists():
            continue
        text = target.read_text(encoding="utf-8").strip()
        if len(text) < min_chars:
            print(f"[SHORT]    summary file may be truncated ({len(text)} chars): {link}")
            print(f"           content: {text!r}")
            issues += 1

    processed_dir = memory_root / "raw" / "processed"
    if processed_dir.exists():
        processed_names = {f.name for f in processed_dir.iterdir() if f.is_file()}
        summarised_names = {Path(p).name for p in all_typed}
        orphaned_raw = processed_names - summarised_names
        for name in sorted(orphaned_raw):
            print(
                f"[RAW_ONLY] processed raw exists but no summary in typed folder: {name}"
            )
            issues += 1

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
