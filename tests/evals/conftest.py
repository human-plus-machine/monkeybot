"""Fixtures for T1 memory structural integrity tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def healthy_memory(tmp_path: Path) -> Path:
    """Valid INDEX.md + episodic file with a long enough summary."""
    root = tmp_path / "mem"
    root.mkdir()
    episodic = root / "episodic"
    episodic.mkdir()
    (episodic / "good.md").write_text(
        "This is a valid memory summary with enough characters.\n",
        encoding="utf-8",
    )
    (root / "INDEX.md").write_text(
        "- [[episodic/good.md]] indexed entry\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def memory_with_orphan(tmp_path: Path) -> Path:
    """INDEX.md references a file that does not exist on disk."""
    root = tmp_path / "mem"
    root.mkdir()
    (root / "episodic").mkdir()
    (root / "INDEX.md").write_text(
        "- [[episodic/does_not_exist.md]] stale link\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def memory_with_unindexed(tmp_path: Path) -> Path:
    """Typed-folder file with no INDEX.md wiki link."""
    root = tmp_path / "mem"
    root.mkdir()
    episodic = root / "episodic"
    episodic.mkdir()
    (episodic / "lonely.md").write_text(
        "This file is long enough but has no INDEX.md entry.\n",
        encoding="utf-8",
    )
    (root / "INDEX.md").write_text("# index with no wiki links\n", encoding="utf-8")
    return root


@pytest.fixture
def memory_with_short_summary(tmp_path: Path) -> Path:
    """INDEX-linked file whose body is shorter than the minimum summary length."""
    root = tmp_path / "mem"
    root.mkdir()
    episodic = root / "episodic"
    episodic.mkdir()
    (episodic / "tiny.md").write_text("short", encoding="utf-8")
    (root / "INDEX.md").write_text(
        "- [[episodic/tiny.md]] indexed but truncated body\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def memory_with_raw_only(tmp_path: Path) -> Path:
    """Processed raw file with no matching typed-folder summary by basename."""
    root = tmp_path / "mem"
    root.mkdir()
    proc = root / "raw" / "processed"
    proc.mkdir(parents=True)
    (proc / "leftover.md").write_text("processed raw without typed pair\n", encoding="utf-8")
    return root


@pytest.fixture
def memory_with_multiple_issues(tmp_path: Path) -> Path:
    """Orphan INDEX link plus an unindexed episodic file."""
    root = tmp_path / "mem"
    root.mkdir()
    episodic = root / "episodic"
    episodic.mkdir()
    (episodic / "extra.md").write_text(
        "This content is long enough to pass short summary check.\n",
        encoding="utf-8",
    )
    (root / "INDEX.md").write_text(
        "- [[episodic/missing.md]] orphan entry only\n",
        encoding="utf-8",
    )
    return root
