"""Tests for @ file-mention pure helpers."""

from __future__ import annotations

from pathlib import Path

from monkeybot_cli.chat_file_index import (
    detect_at_token,
    fuzzy_filter_files,
    list_workspace_files,
)


def test_detect_at_token_at_line_start() -> None:
    assert detect_at_token("@src/foo", 8) == (0, "src/foo")


def test_detect_at_token_after_whitespace() -> None:
    line = "see @readme.md"
    assert detect_at_token(line, len(line)) == (4, "readme.md")


def test_detect_at_token_email_never_triggers() -> None:
    line = "contact a@b.com please"
    assert detect_at_token(line, len("contact a@b.com")) is None


def test_detect_at_token_no_at_present() -> None:
    assert detect_at_token("hello world", 5) is None


def test_detect_at_token_only_matches_current_token() -> None:
    line = "@one two"
    # Cursor past the first token, no trailing @ under the cursor.
    assert detect_at_token(line, len(line)) is None


def test_list_workspace_files_fallback_walk(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x")
    (tmp_path / "README.md").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("x")

    files = list_workspace_files(tmp_path)

    assert "src/main.py" in files
    assert "README.md" in files
    assert not any(f.startswith(".git/") for f in files)
    assert not any(f.startswith("node_modules/") for f in files)


def test_list_workspace_files_respects_limit(tmp_path: Path) -> None:
    for i in range(20):
        (tmp_path / f"file_{i}.txt").write_text("x")

    files = list_workspace_files(tmp_path, limit=5)

    assert len(files) <= 5


def test_fuzzy_filter_files_orders_basename_prefix_first() -> None:
    files = ["a/readme.md", "readme.md", "b/other_readme.md"]
    results = fuzzy_filter_files(files, "readme")
    assert results[0] == "readme.md"


def test_fuzzy_filter_files_subsequence_match() -> None:
    files = ["src/chat_tui.py", "src/other.py"]
    results = fuzzy_filter_files(files, "ctp")
    assert "src/chat_tui.py" in results


def test_fuzzy_filter_files_empty_query_returns_prefix() -> None:
    files = ["a", "b", "c"]
    assert fuzzy_filter_files(files, "", limit=2) == ["a", "b"]


def test_fuzzy_filter_files_limit_enforced() -> None:
    files = [f"file_{i}.txt" for i in range(20)]
    results = fuzzy_filter_files(files, "file", limit=3)
    assert len(results) == 3
