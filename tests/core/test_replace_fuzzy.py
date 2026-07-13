"""Tests for fuzzy replace_in_file and delete_file."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.tools.workspace_service import (
    WorkspaceError,
    WorkspaceFileService,
    WorkspaceSettings,
)


def test_replace_exact(tmp_path: Path) -> None:
    ws = WorkspaceFileService(tmp_path)
    (tmp_path / "f.txt").write_text("alpha beta gamma\n", encoding="utf-8")
    out = ws.replace_in_file("f.txt", "beta", "BETA")
    assert out["ok"] and out["match_mode"] == "exact" and out["replacements"] == 1
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "alpha BETA gamma\n"


def test_replace_line_trimmed_fuzzy(tmp_path: Path) -> None:
    ws = WorkspaceFileService(tmp_path)
    (tmp_path / "f.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    # Caller has wrong indentation on the return line.
    out = ws.replace_in_file("f.py", "def foo():\nreturn 1", "def foo():\n    return 2")
    assert out["ok"]
    assert out["match_mode"] in ("line_trimmed", "indentation_flexible", "whitespace_normalized")
    assert "return 2" in (tmp_path / "f.py").read_text(encoding="utf-8")


def test_replace_all(tmp_path: Path) -> None:
    ws = WorkspaceFileService(tmp_path)
    (tmp_path / "f.txt").write_text("aa aa aa\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="not unique"):
        ws.replace_in_file("f.txt", "aa", "bb")
    out = ws.replace_in_file("f.txt", "aa", "bb", replace_all=True)
    assert out["replacements"] == 3
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "bb bb bb\n"


def test_replace_disproportionate_rejected(tmp_path: Path) -> None:
    ws = WorkspaceFileService(tmp_path)
    big = "line0\n" + "\n".join(f"body{i}" for i in range(20)) + "\nlineZ\n"
    (tmp_path / "f.txt").write_text(big, encoding="utf-8")
    # Anchors match first/last but middle is huge vs old_string — should not match via
    # line-trimmed block of only two lines; missing exact → not found.
    with pytest.raises(WorkspaceError, match="not found"):
        ws.replace_in_file("f.txt", "line0\nMISSING\nlineZ", "x")


def test_delete_file(tmp_path: Path) -> None:
    ws = WorkspaceFileService(tmp_path)
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    out = ws.delete_file("x.txt")
    assert out["ok"]
    assert not (tmp_path / "x.txt").exists()


def test_delete_respects_write_scope(tmp_path: Path) -> None:
    scoped = tmp_path / "allowed"
    scoped.mkdir()
    (scoped / "ok.txt").write_text("a", encoding="utf-8")
    (tmp_path / "other.txt").write_text("b", encoding="utf-8")
    ws = WorkspaceFileService(
        tmp_path,
        settings=WorkspaceSettings(WORKSPACE_WRITE_SCOPE_REL="allowed"),
    )
    ws.delete_file("allowed/ok.txt")
    with pytest.raises(WorkspaceError, match="limited"):
        ws.delete_file("other.txt")
