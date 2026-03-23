"""Workspace write scope: WORKSPACE_WRITE_SCOPE_REL."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.core.workspace_service import WorkspaceError, WorkspaceFileService, WorkspaceSettings


def test_write_allowed_inside_scope() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "memory").mkdir(parents=True)
        (root / "data" / "agent-contract").mkdir(parents=True)
        svc = WorkspaceFileService(
            root,
            settings=WorkspaceSettings(WORKSPACE_WRITE_SCOPE_REL="data/memory"),
        )
        out = svc.write_file("data/memory/hello.txt", "hi")
        assert out["ok"] is True
        assert (root / "data" / "memory" / "hello.txt").read_text() == "hi"


def test_write_rejected_outside_scope() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "memory").mkdir(parents=True)
        (root / "other").mkdir(parents=True)
        svc = WorkspaceFileService(
            root,
            settings=WorkspaceSettings(WORKSPACE_WRITE_SCOPE_REL="data/memory"),
        )
        with pytest.raises(WorkspaceError) as exc:
            svc.write_file("other/x.txt", "nope")
        assert exc.value.code == "write_outside_scope"


def test_replace_rejected_outside_scope() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "memory").mkdir(parents=True)
        p = root / "other" / "f.txt"
        p.parent.mkdir(parents=True)
        p.write_text("old", encoding="utf-8")
        svc = WorkspaceFileService(
            root,
            settings=WorkspaceSettings(WORKSPACE_WRITE_SCOPE_REL="data/memory"),
        )
        with pytest.raises(WorkspaceError) as exc:
            svc.replace_in_file("other/f.txt", "old", "new")
        assert exc.value.code == "write_outside_scope"


def test_no_scope_allows_anywhere_under_repo() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "anywhere").mkdir()
        svc = WorkspaceFileService(root, settings=WorkspaceSettings())
        out = svc.write_file("anywhere/a.txt", "x")
        assert out["ok"] is True
