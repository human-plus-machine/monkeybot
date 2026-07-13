"""Unit tests for Codex-style apply_patch parse / derive / apply."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.tools.patch import (
    AddHunk,
    DeleteHunk,
    PatchError,
    UpdateChunk,
    UpdateHunk,
    derive_new_contents,
    parse_patch,
    plan_and_apply_patch,
    seek_sequence,
)
from monkeybot.core.tools.workspace_service import WorkspaceError, WorkspaceFileService


def test_parse_add_update_delete_move() -> None:
    text = """*** Begin Patch
*** Add File: hello.txt
+Hello world
*** Update File: src/app.py
*** Move to: src/main.py
@@ def greet():
-print("Hi")
+print("Hello, world!")
*** Delete File: obsolete.txt
*** End Patch
"""
    hunks = parse_patch(text)
    assert len(hunks) == 3
    assert isinstance(hunks[0], AddHunk)
    assert hunks[0].path == "hello.txt"
    assert hunks[0].contents == "Hello world"
    assert isinstance(hunks[1], UpdateHunk)
    assert hunks[1].path == "src/app.py"
    assert hunks[1].move_path == "src/main.py"
    assert len(hunks[1].chunks) == 1
    assert hunks[1].chunks[0].change_context == "def greet():"
    assert isinstance(hunks[2], DeleteHunk)
    assert hunks[2].path == "obsolete.txt"


def test_parse_missing_markers() -> None:
    with pytest.raises(PatchError, match="Begin/End"):
        parse_patch("*** Add File: x\n+y\n")


def test_parse_empty_patch() -> None:
    with pytest.raises(PatchError, match="empty"):
        parse_patch("*** Begin Patch\n*** End Patch\n")


def test_seek_sequence_whitespace() -> None:
    lines = ["  def foo():  ", "    return 1"]
    assert seek_sequence(lines, ["def foo():"], 0) == 0
    assert seek_sequence(lines, ["return 1"], 0) == 1


def test_derive_new_contents_exact() -> None:
    original = "a\nb\nc\n"
    chunks = (
        UpdateChunk(old_lines=("b",), new_lines=("B",)),
    )
    out = derive_new_contents("f.txt", chunks, original)
    assert out == "a\nB\nc\n"


def test_derive_new_contents_context_and_fail() -> None:
    original = "def a():\n  x\ndef b():\n  y\n"
    chunks = (
        UpdateChunk(
            old_lines=("  y",),
            new_lines=("  Y",),
            change_context="def b():",
        ),
    )
    out = derive_new_contents("f.py", chunks, original)
    assert "  Y" in out
    bad = (
        UpdateChunk(
            old_lines=("missing",),
            new_lines=("x",),
        ),
    )
    with pytest.raises(PatchError, match="Failed to find"):
        derive_new_contents("f.py", bad, original)


def test_plan_and_apply_multi_file_fail_closed(tmp_path: Path) -> None:
    ws = WorkspaceFileService(tmp_path)
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    (tmp_path / "old.txt").write_text("old\n", encoding="utf-8")

    # Bad update path should leave disk unchanged.
    bad = [
        AddHunk(path="new.txt", contents="n"),
        UpdateHunk(
            path="missing.txt",
            chunks=(UpdateChunk(old_lines=("a",), new_lines=("b",)),),
        ),
    ]
    with pytest.raises(PatchError):
        plan_and_apply_patch(ws, bad)
    assert not (tmp_path / "new.txt").exists()
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "keep\n"

    good_patch = """*** Begin Patch
*** Add File: new.txt
+hello
*** Update File: old.txt
@@
-old
+new
*** Delete File: keep.txt
*** End Patch
"""
    result = plan_and_apply_patch(ws, parse_patch(good_patch))
    assert result["ok"] is True
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello\n"
    assert (tmp_path / "old.txt").read_text(encoding="utf-8") == "new\n"
    assert not (tmp_path / "keep.txt").exists()


def test_apply_move(tmp_path: Path) -> None:
    ws = WorkspaceFileService(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: a.py
*** Move to: b.py
@@
-x = 1
+x = 2
*** End Patch
"""
    plan_and_apply_patch(ws, parse_patch(patch))
    assert not (tmp_path / "a.py").exists()
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "x = 2\n"


def test_mid_apply_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = WorkspaceFileService(tmp_path)
    (tmp_path / "old.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")

    planned_ok = parse_patch(
        """*** Begin Patch
*** Add File: new.txt
+hello
*** Update File: old.txt
@@
-old
+new
*** Delete File: keep.txt
*** End Patch
"""
    )
    # Force failure on the second write by wrapping write_file.
    real_write = ws.write_file
    calls = {"n": 0}

    def flaky_write(path: str, content: str):
        calls["n"] += 1
        if calls["n"] == 2:
            raise WorkspaceError("disk full", code="write_failed")
        return real_write(path, content)

    monkeypatch.setattr(ws, "write_file", flaky_write)
    with pytest.raises(PatchError, match="disk full"):
        plan_and_apply_patch(ws, planned_ok)
    # First add rolled back; originals preserved.
    assert not (tmp_path / "new.txt").exists()
    assert (tmp_path / "old.txt").read_text(encoding="utf-8") == "old\n"
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "keep\n"
