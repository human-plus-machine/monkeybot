"""Subagent writes belong to the parent session that can undo them."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.tools.change_recorder import (
    ChangeStore,
    capture_preimages,
    commit_file_changes,
)
from monkeybot.core.tools.workspace_service import WorkspaceFileService

_CHILD = "subagent:parent1:abc1234567"


def _record_child_write(root: Path) -> None:
    workspace = WorkspaceFileService(root)
    pre = capture_preimages(workspace, "write_file", {"path": "child.txt"})
    workspace.write_file("child.txt", "from the child\n")
    commit_file_changes(
        workspace,
        pre,
        session_id=_CHILD,
        request_id="req-child",
        call_id="c-child",
    )


@pytest.mark.case("T2-SUB-18")
def test_parent_lists_a_subagent_write(tmp_path: Path) -> None:
    _record_child_write(tmp_path)
    listed = ChangeStore(tmp_path).list_changes("parent1")
    assert [entry["path"] for entry in listed] == ["child.txt"]


@pytest.mark.case("T2-SUB-18")
def test_deleting_the_parent_removes_subagent_changes(tmp_path: Path) -> None:
    _record_child_write(tmp_path)
    ChangeStore(tmp_path).delete_session("parent1")
    assert not (tmp_path / ".monkeybot" / "changes" / _CHILD).exists()
