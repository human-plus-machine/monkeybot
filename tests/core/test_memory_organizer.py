"""Tests for MemoryOrganizer memory post-processor."""
from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.llm.provider import Done, TextDelta, UsageEvent
from monkeybot.core.memory.organizer import (
    BUILT_IN_FOLDERS,
    IndexEntry,
    MemoryOrganizer,
)
from monkeybot.core.testing.mocks_provider import fake_provider_prompt_tokens


class FakeProvider:
    name = "fake"
    supports_streaming = True

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def stream(self, messages, tools, *, model: str):
        if not self._responses:
            raise RuntimeError("LLM unavailable")
        text = self._responses.pop(0)
        yield TextDelta(text=text)
        yield UsageEvent(input_tokens=1, output_tokens=1, cached_tokens=0)
        yield Done()

    async def count_input_tokens(self, messages, tools, *, model: str):
        del model
        return fake_provider_prompt_tokens(messages, tools)


def make_organizer(
    tmp_path: Path,
    *,
    provider: FakeProvider | None = None,
    model: str = "gemini-2.0-flash",
    custom_folders=None,
) -> MemoryOrganizer:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(exist_ok=True)
    return MemoryOrganizer(
        provider=provider or FakeProvider([]),
        model=model,
        memory_dir=memory_dir,
        custom_folders=custom_folders,
    )


class TestMemoryOrganizerRunEmpty:
    @pytest.mark.asyncio
    async def test_empty_raw_dir_returns_zero_result(self, tmp_path):
        organizer = make_organizer(tmp_path, provider=FakeProvider([]))
        organizer.raw_dir.mkdir(parents=True)
        result = await organizer.run()
        assert result.files_processed == 0
        assert result.files_written == 0
        assert result.index_updated is False
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_missing_raw_dir_returns_zero_result(self, tmp_path):
        organizer = make_organizer(tmp_path, provider=FakeProvider([]))
        result = await organizer.run()
        assert result.files_processed == 0
        assert result.files_written == 0
        assert result.index_updated is False


class TestMemoryOrganizerRunProcessing:
    @pytest.mark.asyncio
    async def test_processes_raw_file_and_moves_to_processed(self, tmp_path):
        organizer = make_organizer(
            tmp_path,
            provider=FakeProvider([
                "Summary text.",
                "episodic",
                "tags: event\nsummary: Test event happened",
            ]),
        )
        organizer.raw_dir.mkdir(parents=True)
        raw_file = organizer.raw_dir / "2026-03-18-run.md"
        raw_file.write_text("Agent did something today.")

        result = await organizer.run()

        assert result.files_processed == 1
        assert result.files_written == 1
        assert result.index_updated is True
        assert not raw_file.exists()
        assert (organizer.processed_dir / raw_file.name).exists()
        assert any((organizer.memory_dir / "episodic").glob("*.md"))

    @pytest.mark.asyncio
    async def test_invalid_classify_falls_back_to_episodic(self, tmp_path):
        organizer = make_organizer(
            tmp_path,
            provider=FakeProvider(["Summary.", "UNKNOWN_FOLDER", "tags: x\nsummary: y"]),
        )
        organizer.raw_dir.mkdir(parents=True)
        (organizer.raw_dir / "test.md").write_text("content")
        result = await organizer.run()
        assert result.files_written == 1
        assert result.errors == []
        assert any((organizer.memory_dir / "episodic").glob("*.md"))

    @pytest.mark.asyncio
    async def test_file_error_captured_not_raised(self, tmp_path):
        organizer = make_organizer(tmp_path, provider=FakeProvider([]))
        organizer.raw_dir.mkdir(parents=True)
        (organizer.raw_dir / "test.md").write_text("content")
        result = await organizer.run()
        assert result.files_processed == 1
        assert result.files_written == 0
        assert len(result.errors) == 1

    @pytest.mark.asyncio
    async def test_processes_multiple_files(self, tmp_path):
        responses: list[str] = []
        for _ in range(2):
            responses.extend(["Summary.", "episodic", "tags: x\nsummary: y"])
        organizer = make_organizer(tmp_path, provider=FakeProvider(responses))
        organizer.raw_dir.mkdir(parents=True)
        (organizer.raw_dir / "a.md").write_text("content a")
        (organizer.raw_dir / "b.md").write_text("content b")
        result = await organizer.run()
        assert result.files_processed == 2
        assert result.files_written == 2


class TestMemoryOrganizerCustomFolders:
    @pytest.mark.asyncio
    async def test_custom_folder_routing(self, tmp_path):
        from monkeybot.core.config import CustomMemoryFolder

        custom = [CustomMemoryFolder("campaigns", "Marketing campaign data")]
        organizer = make_organizer(
            tmp_path,
            provider=FakeProvider(
                ["Campaign summary.", "campaigns", "tags: campaign\nsummary: Q1 launch"]
            ),
            custom_folders=custom,
        )
        organizer.raw_dir.mkdir(parents=True)
        (organizer.raw_dir / "test.md").write_text("campaign content")
        result = await organizer.run()
        assert result.files_written == 1
        assert any((organizer.memory_dir / "campaigns").glob("*.md"))

    def test_all_folders_includes_custom(self, tmp_path):
        from monkeybot.core.config import CustomMemoryFolder

        custom = [CustomMemoryFolder("sprints", "Sprint goals")]
        organizer = make_organizer(tmp_path, provider=FakeProvider([]), custom_folders=custom)
        assert "sprints" in organizer._all_folders
        assert all(f in organizer._all_folders for f in BUILT_IN_FOLDERS)


class TestUpdateIndex:
    @pytest.mark.asyncio
    async def test_creates_index_if_missing(self, tmp_path):
        organizer = make_organizer(
            tmp_path,
            provider=FakeProvider(["tags: event\nsummary: Test event happened"]),
        )
        entries = [IndexEntry("episodic", "2026-03-18-event.md", "test", "Test event")]
        await organizer._update_index(entries)
        assert organizer.index_path.exists()
        content = organizer.index_path.read_text()
        assert "## episodic/" in content
        assert "2026-03-18-event.md" in content

    @pytest.mark.asyncio
    async def test_appends_to_existing_section(self, tmp_path):
        organizer = make_organizer(
            tmp_path,
            provider=FakeProvider(["tags: new\nsummary: New event"]),
        )
        organizer.index_path.write_text(
            "# Memory Index\n\n## episodic/\n- [[episodic/old.md]] | tags: old | Old event\n"
        )
        entries = [IndexEntry("episodic", "new.md", "new", "New event")]
        await organizer._update_index(entries)
        content = organizer.index_path.read_text()
        assert "old.md" in content
        assert "new.md" in content

    @pytest.mark.asyncio
    async def test_creates_new_section_if_missing(self, tmp_path):
        organizer = make_organizer(
            tmp_path,
            provider=FakeProvider(["tags: data\nsummary: Campaign result"]),
        )
        organizer.index_path.write_text("# Memory Index\n\n## episodic/\n")
        entries = [IndexEntry("semantic", "fact.md", "data", "Campaign result")]
        await organizer._update_index(entries)
        content = organizer.index_path.read_text()
        assert "## semantic/" in content
        assert "fact.md" in content
