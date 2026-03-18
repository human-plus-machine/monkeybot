"""Tests for LLMCouncil memory post-processor."""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.core.council import LLMCouncil, CouncilResult, CouncilError, BUILT_IN_FOLDERS, IndexEntry


def make_model(responses: list[str] | None = None):
    """MockChatModel that returns responses in sequence."""
    model = MagicMock()
    if responses:
        model.ainvoke = AsyncMock(side_effect=[
            MagicMock(content=r) for r in responses
        ])
    else:
        model.ainvoke = AsyncMock(return_value=MagicMock(content="episodic"))
    return model


def make_council(tmp_path: Path, model=None, custom_folders=None) -> LLMCouncil:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(exist_ok=True)
    return LLMCouncil(
        model=model or make_model(),
        memory_dir=memory_dir,
        custom_folders=custom_folders,
    )


class TestCouncilRunEmpty:
    @pytest.mark.asyncio
    async def test_empty_raw_dir_returns_zero_result(self, tmp_path):
        council = make_council(tmp_path)
        council.raw_dir.mkdir(parents=True)
        result = await council.run()
        assert result.files_processed == 0
        assert result.files_written == 0
        assert result.index_updated is False
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_missing_raw_dir_returns_zero_result(self, tmp_path):
        council = make_council(tmp_path)
        # raw_dir never created
        result = await council.run()
        assert result.files_processed == 0
        assert result.files_written == 0
        assert result.index_updated is False


class TestCouncilRunProcessing:
    @pytest.mark.asyncio
    async def test_processes_raw_file_and_moves_to_processed(self, tmp_path):
        council = make_council(tmp_path, model=make_model([
            "Summary text.",
            "episodic",
            "tags: event\nsummary: Test event happened",
        ]))
        council.raw_dir.mkdir(parents=True)
        raw_file = council.raw_dir / "2026-03-18-run.md"
        raw_file.write_text("Agent did something today.")

        result = await council.run()

        assert result.files_processed == 1
        assert result.files_written == 1
        assert result.index_updated is True
        assert not raw_file.exists()
        assert (council.processed_dir / raw_file.name).exists()
        assert any((council.memory_dir / "episodic").glob("*.md"))

    @pytest.mark.asyncio
    async def test_invalid_classify_falls_back_to_episodic(self, tmp_path):
        council = make_council(tmp_path, model=make_model([
            "Summary.", "UNKNOWN_FOLDER", "tags: x\nsummary: y"
        ]))
        council.raw_dir.mkdir(parents=True)
        (council.raw_dir / "test.md").write_text("content")
        result = await council.run()
        assert result.files_written == 1
        assert result.errors == []
        assert any((council.memory_dir / "episodic").glob("*.md"))

    @pytest.mark.asyncio
    async def test_file_error_captured_not_raised(self, tmp_path):
        model = make_model()
        model.ainvoke = AsyncMock(side_effect=Exception("LLM unavailable"))
        council = make_council(tmp_path, model=model)
        council.raw_dir.mkdir(parents=True)
        (council.raw_dir / "test.md").write_text("content")
        result = await council.run()
        assert result.files_processed == 1
        assert result.files_written == 0
        assert len(result.errors) == 1

    @pytest.mark.asyncio
    async def test_processes_multiple_files(self, tmp_path):
        responses = []
        for _ in range(2):
            responses.extend(["Summary.", "episodic", "tags: x\nsummary: y"])
        council = make_council(tmp_path, model=make_model(responses))
        council.raw_dir.mkdir(parents=True)
        (council.raw_dir / "a.md").write_text("content a")
        (council.raw_dir / "b.md").write_text("content b")
        result = await council.run()
        assert result.files_processed == 2
        assert result.files_written == 2


class TestCouncilCustomFolders:
    @pytest.mark.asyncio
    async def test_custom_folder_routing(self, tmp_path):
        from src.core.config import CustomMemoryFolder
        custom = [CustomMemoryFolder("campaigns", "Marketing campaign data")]
        council = make_council(tmp_path, model=make_model([
            "Campaign summary.", "campaigns", "tags: campaign\nsummary: Q1 launch"
        ]), custom_folders=custom)
        council.raw_dir.mkdir(parents=True)
        (council.raw_dir / "test.md").write_text("campaign content")
        result = await council.run()
        assert result.files_written == 1
        assert any((council.memory_dir / "campaigns").glob("*.md"))

    def test_all_folders_includes_custom(self, tmp_path):
        from src.core.config import CustomMemoryFolder
        custom = [CustomMemoryFolder("sprints", "Sprint goals")]
        council = make_council(tmp_path, custom_folders=custom)
        assert "sprints" in council._all_folders
        assert all(f in council._all_folders for f in BUILT_IN_FOLDERS)


class TestUpdateIndex:
    @pytest.mark.asyncio
    async def test_creates_index_if_missing(self, tmp_path):
        council = make_council(tmp_path, model=make_model(
            ["tags: event\nsummary: Test event happened"]
        ))
        entries = [IndexEntry("episodic", "2026-03-18-event.md", "test", "Test event")]
        await council._update_index(entries)
        assert council.index_path.exists()
        content = council.index_path.read_text()
        assert "## episodic/" in content
        assert "2026-03-18-event.md" in content

    @pytest.mark.asyncio
    async def test_appends_to_existing_section(self, tmp_path):
        council = make_council(tmp_path, model=make_model(
            ["tags: new\nsummary: New event"]
        ))
        council.index_path.write_text(
            "# Memory Index\n\n## episodic/\n- [[episodic/old.md]] | tags: old | Old event\n"
        )
        entries = [IndexEntry("episodic", "new.md", "new", "New event")]
        await council._update_index(entries)
        content = council.index_path.read_text()
        assert "old.md" in content
        assert "new.md" in content

    @pytest.mark.asyncio
    async def test_creates_new_section_if_missing(self, tmp_path):
        council = make_council(tmp_path, model=make_model(
            ["tags: data\nsummary: Campaign result"]
        ))
        council.index_path.write_text("# Memory Index\n\n## episodic/\n")
        entries = [IndexEntry("semantic", "fact.md", "data", "Campaign result")]
        await council._update_index(entries)
        content = council.index_path.read_text()
        assert "## semantic/" in content
        assert "fact.md" in content
