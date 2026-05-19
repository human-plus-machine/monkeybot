"""T1 — structural integrity of on-disk markdown memory trees."""

from __future__ import annotations

from pathlib import Path

from monkeybot.core.memory.integrity import MemoryIntegrityChecker


def test_healthy_snapshot_passes(healthy_memory: Path) -> None:
    result = MemoryIntegrityChecker(healthy_memory).run()
    assert result.total_issues == 0


def test_orphan_detected(memory_with_orphan: Path) -> None:
    result = MemoryIntegrityChecker(memory_with_orphan).run()
    assert result.orphan_count == 1
    assert result.unindexed_count == 0


def test_unindexed_file_detected(memory_with_unindexed: Path) -> None:
    result = MemoryIntegrityChecker(memory_with_unindexed).run()
    assert result.unindexed_count == 1
    assert result.orphan_count == 0


def test_short_summary_detected(memory_with_short_summary: Path) -> None:
    result = MemoryIntegrityChecker(memory_with_short_summary).run()
    assert result.short_summary_count == 1


def test_raw_only_detected(memory_with_raw_only: Path) -> None:
    result = MemoryIntegrityChecker(memory_with_raw_only).run()
    assert result.raw_only_count == 1


def test_multiple_issues_summed_correctly(memory_with_multiple_issues: Path) -> None:
    result = MemoryIntegrityChecker(memory_with_multiple_issues).run()
    assert result.orphan_count == 1
    assert result.unindexed_count == 1
    assert result.total_issues == 2


def test_empty_dir_no_issues(tmp_path: Path) -> None:
    root = tmp_path / "empty_mem"
    root.mkdir()
    result = MemoryIntegrityChecker(root).run()
    assert result.total_issues == 0


def test_missing_dir_no_issues(tmp_path: Path) -> None:
    missing = tmp_path / "no_such_memory_dir"
    result = MemoryIntegrityChecker(missing).run()
    assert result.total_issues == 0


def test_counts_reported_correctly(healthy_memory: Path) -> None:
    result = MemoryIntegrityChecker(healthy_memory).run()
    assert result.total_index_entries == 1
    assert result.total_typed_files == 1
