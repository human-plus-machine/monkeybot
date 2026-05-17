"""Tests for ``monkeybot.core.memory`` storage operations."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.memory import (
    INDEX_FILENAME,
    MemoryPromotionError,
    async_load_index,
    async_promote_to_memory,
    async_search_memory_files,
)
from monkeybot.core.memory.storage_ops import async_search_memory
from monkeybot.core.workspace import create_workspace_storage


def _st(root: Path):
    return create_workspace_storage("local://" + str(root.resolve()))


@pytest.mark.asyncio
async def test_load_index_returns_lines_for_valid_file(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / INDEX_FILENAME).write_text("a\n b\n", encoding="utf-8")

    lines = await async_load_index(_st(memory))

    assert lines == ["a", "b"]


@pytest.mark.asyncio
async def test_load_index_returns_empty_when_memory_path_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nosuch"

    lines = await async_load_index(_st(missing))

    assert lines == []


@pytest.mark.asyncio
async def test_load_index_returns_empty_when_index_missing(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()

    lines = await async_load_index(_st(memory))

    assert lines == []


@pytest.mark.asyncio
async def test_load_index_unicode_strict_raises_on_invalid_utf8(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / INDEX_FILENAME).write_bytes(b"\xff\xfe")

    with pytest.raises(UnicodeDecodeError):
        await async_load_index(_st(memory))


@pytest.mark.asyncio
async def test_search_memory_deterministic_case_insensitive(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / INDEX_FILENAME).write_text("Alpha\nbeta\nAlpha beta\n", encoding="utf-8")

    result = await async_search_memory("alpha", _st(memory), top_k=5)

    assert result == ["Alpha", "Alpha beta"]


@pytest.mark.asyncio
async def test_search_memory_multi_token_and(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / INDEX_FILENAME).write_text("foo bar\nfoo\nbar\n", encoding="utf-8")

    result = await async_search_memory("foo bar", _st(memory))

    assert result == ["foo bar"]


@pytest.mark.asyncio
async def test_search_memory_respects_top_k_in_file_order(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    content = (
        "\n".join(["match one", "match two", "match three", "match four", "match five"]) + "\n"
    )
    (memory / INDEX_FILENAME).write_text(content, encoding="utf-8")

    got = await async_search_memory("match", _st(memory), top_k=2)

    assert got == ["match one", "match two"]


@pytest.mark.asyncio
async def test_search_memory_empty_query_returns_empty(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / INDEX_FILENAME).write_text("anything\n", encoding="utf-8")

    got = await async_search_memory("", _st(memory))

    assert got == []


@pytest.mark.asyncio
async def test_search_memory_non_positive_top_k_returns_empty(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / INDEX_FILENAME).write_text("x\n", encoding="utf-8")

    got = await async_search_memory("x", _st(memory), top_k=0)

    assert got == []


@pytest.mark.asyncio
async def test_search_memory_files_includes_raw_by_default(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    raw = memory / "raw"
    raw.mkdir(parents=True)
    (raw / "post.md").write_text('echo "git -C app status"\n', encoding="utf-8")

    got = await async_search_memory_files(_st(memory), "git -C app status", max_hits=10)

    assert len(got["hits"]) == 1
    assert got["hits"][0]["path"].replace("\\", "/") == "raw/post.md"


@pytest.mark.asyncio
async def test_search_memory_files_can_skip_raw_tree(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    semantic = memory / "semantic"
    semantic.mkdir(parents=True)
    (semantic / "note.md").write_text(
        "Run `git -C app remote -v` to list remotes for the app checkout.\n", encoding="utf-8"
    )
    raw = memory / "raw"
    raw.mkdir(parents=True)
    (raw / "post.md").write_text(
        'tool: run_command\n```json\n{"command": "git -C app remote -v"}\n```\n',
        encoding="utf-8",
    )

    st = _st(memory)
    all_hits = await async_search_memory_files(st, "git -C app remote -v", max_hits=10)
    paths_all = {h["path"].replace("\\", "/") for h in all_hits["hits"]}
    assert "raw/post.md" in paths_all
    assert "semantic/note.md" in paths_all

    filtered = await async_search_memory_files(
        st,
        "git -C app remote -v",
        max_hits=10,
        skip_relative_prefixes=("raw",),
    )
    paths_f = [h["path"].replace("\\", "/") for h in filtered["hits"]]
    assert paths_f == ["semantic/note.md"]


@pytest.mark.asyncio
async def test_promote_to_memory_moves_into_semantic(tmp_path: Path) -> None:
    run_id = "01TEST"
    runs_root = tmp_path / "runs"
    promos = runs_root / run_id / "memory-promotions"
    promos.mkdir(parents=True)
    src = promos / "x.md"
    src.write_text("body", encoding="utf-8")
    memory = tmp_path / "memory_root"
    memory.mkdir()

    await async_promote_to_memory(run_id, src, _st(memory))

    dest = memory / "semantic" / "x.md"
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") == "body"
    assert not src.exists()


@pytest.mark.asyncio
async def test_promote_to_memory_rejects_file_outside_run_id(tmp_path: Path) -> None:
    foreign = tmp_path / "other" / "x.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("", encoding="utf-8")

    with pytest.raises(MemoryPromotionError):
        await async_promote_to_memory("01TEST", foreign, _st(tmp_path / "memory"))


@pytest.mark.asyncio
async def test_promote_to_memory_rejects_non_file(tmp_path: Path) -> None:
    run_id = "01TEST"
    d = tmp_path / "runs" / run_id / "dir"
    d.mkdir(parents=True)

    with pytest.raises(MemoryPromotionError):
        await async_promote_to_memory(run_id, d, _st(tmp_path / "memory"))
