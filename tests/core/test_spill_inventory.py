"""Tests for session-scoped spill cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.tools.spill_inventory import (
    cleanup_session_spill_files,
    session_spill_dirs,
)


def test_session_spill_dirs_includes_parent_and_subagent(tmp_path: Path) -> None:
    root = tmp_path / ".monkeybot" / "spill"
    (root / "sess-1").mkdir(parents=True)
    (root / "subagent:sess-1:aaa").mkdir(parents=True)
    (root / "subagent:sess-1:bbb").mkdir(parents=True)
    (root / "sess-2").mkdir(parents=True)
    (root / "subagent:sess-2:ccc").mkdir(parents=True)

    dirs = session_spill_dirs(tmp_path, "sess-1")
    names = {p.name for p in dirs}
    assert names == {"sess-1", "subagent:sess-1:aaa", "subagent:sess-1:bbb"}


@pytest.mark.asyncio
async def test_cleanup_session_spill_files_concurrent(tmp_path: Path) -> None:
    root = tmp_path / ".monkeybot" / "spill"
    for name in ("s1", "subagent:s1:one", "subagent:s1:two", "other"):
        d = root / name
        d.mkdir(parents=True)
        (d / "x.txt").write_text("data", encoding="utf-8")

    await cleanup_session_spill_files(tmp_path, "s1")

    assert not (root / "s1").exists()
    assert not (root / "subagent:s1:one").exists()
    assert not (root / "subagent:s1:two").exists()
    assert (root / "other" / "x.txt").read_text(encoding="utf-8") == "data"
