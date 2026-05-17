"""Workspace paths that use symlinks (playground ``workspace/data`` → ``../data``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.tools.workspace_service import WorkspaceError, WorkspaceFileService


def test_list_directory_includes_symlink_children(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("hello", encoding="utf-8")
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    (ws_root / "data").symlink_to(outside, target_is_directory=True)

    svc = WorkspaceFileService(ws_root)
    entries = {e["name"]: e for e in svc.list_directory(None)}
    assert "data" in entries
    assert entries["data"]["kind"] == "dir"
    assert entries["data"]["path"] == "data"

    nested = svc.list_directory("data")
    names = {e["name"] for e in nested}
    assert "note.txt" in names


def test_read_file_through_symlink_prefix(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("hello\n", encoding="utf-8")
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    (ws_root / "data").symlink_to(outside, target_is_directory=True)

    svc = WorkspaceFileService(ws_root)
    out = svc.read_file("data/note.txt", offset=1, limit=10)
    assert "hello" in out["content"]


def test_reject_parent_escape(tmp_path: Path) -> None:
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    svc = WorkspaceFileService(ws_root)
    with pytest.raises(WorkspaceError) as exc:
        svc.read_file("../outside", offset=1, limit=10)
    assert exc.value.code == "invalid_path"
