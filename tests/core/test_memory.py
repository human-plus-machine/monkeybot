"""Tests for ``monkeybot.core.memory``."""

from __future__ import annotations

from pathlib import Path

import pytest
from monkeybot.core.memory import (
    INDEX_FILENAME,
    MemoryPromotionError,
    load_index,
    promote_to_memory,
    search_memory,
)


@pytest.mark.asyncio
async def test_load_index_returns_lines_for_valid_file(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / INDEX_FILENAME).write_text("a\n b\n", encoding="utf-8")

    lines = await load_index(memory)

    assert lines == ["a", "b"]


@pytest.mark.asyncio
async def test_load_index_returns_empty_when_memory_path_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nosuch"

    lines = await load_index(missing)

    assert lines == []


@pytest.mark.asyncio
async def test_load_index_returns_empty_when_index_missing(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()

    lines = await load_index(memory)

    assert lines == []


@pytest.mark.asyncio
async def test_load_index_unicode_strict_raises_on_invalid_utf8(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / INDEX_FILENAME).write_bytes(b"\xff\xfe")

    with pytest.raises(UnicodeDecodeError):
        await load_index(memory)


@pytest.mark.asyncio
async def test_search_memory_deterministic_case_insensitive(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / INDEX_FILENAME).write_text("Alpha\nbeta\nAlpha beta\n", encoding="utf-8")

    result = await search_memory("alpha", memory, top_k=5)

    assert result == ["Alpha", "Alpha beta"]


@pytest.mark.asyncio
async def test_search_memory_multi_token_and(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / INDEX_FILENAME).write_text("foo bar\nfoo\nbar\n", encoding="utf-8")

    result = await search_memory("foo bar", memory)

    assert result == ["foo bar"]


@pytest.mark.asyncio
async def test_search_memory_respects_top_k_in_file_order(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    content = (
        "\n".join(["match one", "match two", "match three", "match four", "match five"]) + "\n"
    )
    (memory / INDEX_FILENAME).write_text(content, encoding="utf-8")

    got = await search_memory("match", memory, top_k=2)

    assert got == ["match one", "match two"]


@pytest.mark.asyncio
async def test_search_memory_empty_query_returns_empty(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / INDEX_FILENAME).write_text("anything\n", encoding="utf-8")

    got = await search_memory("", memory)

    assert got == []


@pytest.mark.asyncio
async def test_search_memory_non_positive_top_k_returns_empty(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / INDEX_FILENAME).write_text("x\n", encoding="utf-8")

    got = await search_memory("x", memory, top_k=0)

    assert got == []


@pytest.mark.asyncio
async def test_promote_to_memory_moves_into_semantic(tmp_path: Path) -> None:
    run_id = "01TEST"
    runs_root = tmp_path / "runs"
    promos = runs_root / run_id / "memory-promotions"
    promos.mkdir(parents=True)
    src = promos / "x.md"
    src.write_text("body", encoding="utf-8")
    memory = tmp_path / "memory_root"

    await promote_to_memory(run_id, src, memory)

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
        await promote_to_memory("01TEST", foreign, tmp_path / "memory")


@pytest.mark.asyncio
async def test_promote_to_memory_rejects_non_file(tmp_path: Path) -> None:
    run_id = "01TEST"
    d = tmp_path / "runs" / run_id / "dir"
    d.mkdir(parents=True)

    with pytest.raises(MemoryPromotionError):
        await promote_to_memory(run_id, d, tmp_path / "memory")
