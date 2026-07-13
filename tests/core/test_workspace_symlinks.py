"""Workspace and trusted-skills symlinks must not escape their roots."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.tools.workspace_service import WorkspaceError, WorkspaceFileService


def test_list_directory_rejects_workspace_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("hello", encoding="utf-8")
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    (ws_root / "data").symlink_to(outside, target_is_directory=True)

    svc = WorkspaceFileService(ws_root)
    with pytest.raises(WorkspaceError, match="escapes") as exc:
        svc.list_directory("data")
    assert exc.value.code == "path_escape"


def test_read_file_rejects_workspace_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("hello\n", encoding="utf-8")
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    (ws_root / "data").symlink_to(outside, target_is_directory=True)

    svc = WorkspaceFileService(ws_root)
    with pytest.raises(WorkspaceError, match="escapes") as exc:
        svc.read_file("data/note.txt", offset=1, limit=10)
    assert exc.value.code == "path_escape"


def test_skills_routes_reads_but_rejects_writes_and_escapes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    skills = tmp_path / "skills"
    workspace.mkdir()
    (skills / "demo").mkdir(parents=True)
    (skills / "demo" / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    svc = WorkspaceFileService(workspace, skills_root=skills)

    assert "Demo" in svc.read_file("skills/demo/SKILL.md", limit=10)["content"]
    with pytest.raises(WorkspaceError, match="read-only") as exc:
        svc.write_file("skills/demo/new.md", "nope")
    assert exc.value.code == "skills_read_only"

    outside = tmp_path / "outside"
    outside.mkdir()
    (skills / "escaped").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError) as exc:
        svc.read_file("skills/escaped/nope.md", limit=10)
    assert exc.value.code == "path_escape"


def test_reject_parent_escape(tmp_path: Path) -> None:
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    svc = WorkspaceFileService(ws_root)
    with pytest.raises(WorkspaceError) as exc:
        svc.read_file("../outside", offset=1, limit=10)
    assert exc.value.code == "invalid_path"
