"""Contract tests for :class:`~monkeybot.core.workspace.local.LocalWorkspaceStorage`."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from monkeybot.core.workspace.local import LocalWorkspaceStorage


def _make_local(tmp_path: Path) -> LocalWorkspaceStorage:
    root = tmp_path / "ws"
    root.mkdir()
    return LocalWorkspaceStorage(root)


@pytest.mark.asyncio
async def test_read_write_roundtrip(tmp_path: Path) -> None:
    st = _make_local(tmp_path)
    await st.write_text("hello.txt", "alpha")
    assert await st.read_text("hello.txt") == "alpha"


@pytest.mark.asyncio
async def test_append_text(tmp_path: Path) -> None:
    st = _make_local(tmp_path)
    await st.append_text("log.md", "a")
    await st.append_text("log.md", "b")
    assert await st.read_text("log.md") == "ab"


@pytest.mark.asyncio
async def test_exists(tmp_path: Path) -> None:
    st = _make_local(tmp_path)
    assert await st.exists("nope.txt") is False
    await st.write_text("nope.txt", "")
    assert await st.exists("nope.txt") is True


@pytest.mark.asyncio
async def test_list_files_recursive_forward_slash(tmp_path: Path) -> None:
    st = _make_local(tmp_path)
    await st.write_text("top.md", "1")
    await st.write_text("deep/nested/x.md", "2")

    all_files = await st.list_files("")
    assert sorted(all_files) == ["deep/nested/x.md", "top.md"]


@pytest.mark.asyncio
async def test_list_files_under_raw_includes_processed_subtree(tmp_path: Path) -> None:
    st = _make_local(tmp_path)
    await st.write_text("raw/inbox.md", "q")
    await st.write_text("raw/processed/done.md", "q")

    under_raw = await st.list_files("raw/")
    assert "raw/inbox.md" in under_raw
    assert "raw/processed/done.md" in under_raw


@pytest.mark.asyncio
async def test_delete(tmp_path: Path) -> None:
    st = _make_local(tmp_path)
    await st.write_text("gone.md", "x")
    assert await st.exists("gone.md") is True
    await st.delete("gone.md")
    assert await st.exists("gone.md") is False


@pytest.mark.asyncio
async def test_move_renames(tmp_path: Path) -> None:
    st = _make_local(tmp_path)
    await st.write_text("a.md", "body")
    await st.move("a.md", "b.md")
    assert await st.exists("a.md") is False
    assert await st.read_text("b.md") == "body"


@pytest.mark.asyncio
async def test_gc_prefix_deletes_stale_files_in_prefix_dir(tmp_path: Path) -> None:
    st = _make_local(tmp_path)
    await st.write_text("raw/processed/keep.md", "k")
    await st.write_text("raw/processed/stale.md", "s")

    root = st.root
    stale = root / "raw" / "processed" / "stale.md"
    old = time.time() - 10_000
    await asyncio.to_thread(os.utime, stale, (old, old))

    counts = await st.gc_prefix("raw/processed/", max_age_sec=3600)
    assert counts["scanned"] == 2
    assert counts["deleted"] == 1
    assert stale.exists() is False