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
from monkeybot.core.workspace import create_workspace_storage


class FakeProvider:
    name = "fake"
    supports_streaming = True

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def stream(self, messages, tools, *, model: str, thinking_budget=None):
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
) -> tuple[MemoryOrganizer, Path]:
    memory_root = tmp_path / "memory"
    memory_root.mkdir(exist_ok=True)
    uri = "local://" + str(memory_root.resolve())
    storage = create_workspace_storage(uri)
    org = MemoryOrganizer(
        provider=provider or FakeProvider([]),
        model=model,
        storage=storage,
        custom_folders=custom_folders,
    )
    return org, memory_root


class TestMemoryOrganizerRunEmpty:
    @pytest.mark.asyncio
    async def test_empty_raw_dir_returns_zero_result(self, tmp_path):
        organizer, root = make_organizer(tmp_path, provider=FakeProvider([]))
        (root / "raw").mkdir(parents=True)
        result = await organizer.run()
        assert result.files_processed == 0
        assert result.files_written == 0
        assert result.index_updated is False
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_missing_raw_dir_returns_zero_result(self, tmp_path):
        organizer, _root = make_organizer(tmp_path, provider=FakeProvider([]))
        result = await organizer.run()
        assert result.files_processed == 0
        assert result.files_written == 0
        assert result.index_updated is False


class TestMemoryOrganizerRunProcessing:
    @pytest.mark.asyncio
    async def test_processes_raw_file_and_moves_to_processed(self, tmp_path):
        organizer, root = make_organizer(
            tmp_path,
            provider=FakeProvider([
                "Summary text.",
                "episodic",
                "tags: event\nsummary: Test event happened",
            ]),
        )
        raw = root / "raw"
        raw.mkdir(parents=True)
        raw_file = raw / "2026-03-18-run.md"
        raw_file.write_text("Agent did something today.")

        result = await organizer.run()

        assert result.files_processed == 1
        assert result.files_written == 1
        assert result.index_updated is True
        assert not raw_file.exists()
        assert (root / "raw" / "processed" / raw_file.name).exists()
        assert any((root / "episodic").glob("*.md"))

    @pytest.mark.asyncio
    async def test_invalid_classify_falls_back_to_episodic(self, tmp_path):
        organizer, root = make_organizer(
            tmp_path,
            provider=FakeProvider(["Summary.", "UNKNOWN_FOLDER", "tags: x\nsummary: y"]),
        )
        raw = root / "raw"
        raw.mkdir(parents=True)
        (raw / "test.md").write_text("content")
        result = await organizer.run()
        assert result.files_written == 1
        assert result.errors == []
        assert any((root / "episodic").glob("*.md"))

    @pytest.mark.asyncio
    async def test_file_error_captured_not_raised(self, tmp_path):
        organizer, root = make_organizer(tmp_path, provider=FakeProvider([]))
        raw = root / "raw"
        raw.mkdir(parents=True)
        (raw / "test.md").write_text("content")
        result = await organizer.run()
        assert result.files_processed == 1
        assert result.files_written == 0
        assert len(result.errors) == 1

    @pytest.mark.asyncio
    async def test_processes_multiple_files(self, tmp_path):
        responses: list[str] = []
        for _ in range(2):
            responses.extend(["Summary.", "episodic", "tags: x\nsummary: y"])
        organizer, root = make_organizer(tmp_path, provider=FakeProvider(responses))
        raw = root / "raw"
        raw.mkdir(parents=True)
        (raw / "a.md").write_text("content a")
        (raw / "b.md").write_text("content b")
        result = await organizer.run()
        assert result.files_processed == 2
        assert result.files_written == 2


class TestMemoryOrganizerCustomFolders:
    @pytest.mark.asyncio
    async def test_custom_folder_routing(self, tmp_path):
        from monkeybot.core.config import CustomMemoryFolder

        custom = [CustomMemoryFolder("campaigns", "Marketing campaign data")]
        organizer, root = make_organizer(
            tmp_path,
            provider=FakeProvider(
                ["Campaign summary.", "campaigns", "tags: campaign\nsummary: Q1 launch"]
            ),
            custom_folders=custom,
        )
        raw = root / "raw"
        raw.mkdir(parents=True)
        (raw / "test.md").write_text("campaign content")
        result = await organizer.run()
        assert result.files_written == 1
        assert any((root / "campaigns").glob("*.md"))

    def test_all_folders_includes_custom(self, tmp_path):
        from monkeybot.core.config import CustomMemoryFolder

        custom = [CustomMemoryFolder("sprints", "Sprint goals")]
        organizer, _root = make_organizer(tmp_path, provider=FakeProvider([]), custom_folders=custom)
        assert "sprints" in organizer._all_folders
        assert all(f in organizer._all_folders for f in BUILT_IN_FOLDERS)


class TestMemoryOrganizerLinking:
    @pytest.mark.asyncio
    async def test_links_related_from_index_candidates(self, tmp_path):
        organizer, root = make_organizer(
            tmp_path,
            provider=FakeProvider(
                [
                    "User prefers dark mode in the editor.",
                    "semantic",
                    "related: semantic/theme.md\nsupersedes: none",
                    "tags: prefs\nsummary: Prefers dark mode",
                ]
            ),
        )
        (root / "semantic").mkdir(parents=True)
        (root / "INDEX.md").write_text(
            "# Memory Index\n\n"
            "- [[semantic/theme.md]] | tags: ui | Uses light theme today\n"
            "- [[episodic/other.md]] | tags: noise | Unrelated tool failure\n"
        )
        raw = root / "raw"
        raw.mkdir(parents=True)
        (raw / "pref.md").write_text("User said they want dark mode.")

        result = await organizer.run()
        assert result.files_written == 1
        note = next((root / "semantic").glob("*.md")).read_text()
        assert "[[semantic/theme.md]]" in note
        assert note.startswith("---")
        assert "type: semantic" in note

    @pytest.mark.asyncio
    async def test_rejects_invented_link_paths(self, tmp_path):
        organizer, root = make_organizer(
            tmp_path,
            provider=FakeProvider(
                [
                    "Summary about cats.",
                    "episodic",
                    "related: semantic/made-up.md, episodic/real.md\nsupersedes: none",
                    "tags: cats\nsummary: Cat note",
                ]
            ),
        )
        (root / "INDEX.md").write_text(
            "# Memory Index\n\n"
            "- [[episodic/real.md]] | tags: pets | Prior cat generation\n"
        )
        raw = root / "raw"
        raw.mkdir(parents=True)
        (raw / "cat.md").write_text("generated a cat image")

        await organizer.run()
        note = next((root / "episodic").glob("*.md")).read_text()
        assert "[[episodic/real.md]]" in note
        assert "made-up" not in note

    @pytest.mark.asyncio
    async def test_skips_link_llm_when_index_empty(self, tmp_path):
        # Only three LLM calls: summarize, classify, index entry.
        organizer, root = make_organizer(
            tmp_path,
            provider=FakeProvider(
                [
                    "Summary text.",
                    "episodic",
                    "tags: event\nsummary: Test event happened",
                ]
            ),
        )
        raw = root / "raw"
        raw.mkdir(parents=True)
        (raw / "2026-03-18-run.md").write_text("Agent did something today.")
        result = await organizer.run()
        assert result.files_written == 1


def test_parse_link_decision_filters_and_caps():
    from monkeybot.core.memory.organizer import parse_link_decision

    allowed = {
        "semantic/a.md",
        "semantic/b.md",
        "episodic/c.md",
        "semantic/d.md",
    }
    decision = parse_link_decision(
        "related: [[semantic/a.md]], semantic/b.md, semantic/nope.md, episodic/c.md, semantic/d.md\n"
        "supersedes: semantic/a.md\n",
        allowed=allowed,
    )
    assert decision.supersedes == "semantic/a.md"
    assert "semantic/a.md" not in decision.related
    assert decision.related == ("semantic/b.md", "episodic/c.md", "semantic/d.md")

    @pytest.mark.asyncio
    async def test_creates_index_if_missing(self, tmp_path):
        organizer, root = make_organizer(
            tmp_path,
            provider=FakeProvider(["tags: event\nsummary: Test event happened"]),
        )
        entries = [IndexEntry("episodic", "2026-03-18-event.md", "test", "Test event")]
        await organizer._update_index(entries)
        index_path = root / "INDEX.md"
        assert index_path.exists()
        content = index_path.read_text()
        assert "2026-03-18-event.md" in content
        assert content.strip().startswith("# Memory Index")

    @pytest.mark.asyncio
    async def test_appends_entries_in_recency_order(self, tmp_path):
        organizer, root = make_organizer(
            tmp_path,
            provider=FakeProvider(["tags: new\nsummary: New event"]),
        )
        index_path = root / "INDEX.md"
        index_path.write_text(
            "# Memory Index\n\n- [[episodic/old.md]] | tags: old | Old event\n"
        )
        entries = [IndexEntry("episodic", "new.md", "new", "New event")]
        await organizer._update_index(entries)
        content = index_path.read_text()
        assert "old.md" in content
        assert "new.md" in content
        assert content.index("old.md") < content.index("new.md")

    @pytest.mark.asyncio
    async def test_archives_overflow_when_cap_exceeded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMORY_INDEX_CAP", "2")
        organizer, root = make_organizer(
            tmp_path,
            provider=FakeProvider(["tags: c\nsummary: Third"]),
        )
        index_path = root / "INDEX.md"
        index_path.write_text(
            "# Memory Index\n\n"
            "- [[episodic/a.md]] | tags: a | A\n"
            "- [[episodic/b.md]] | tags: b | B\n"
        )
        entries = [IndexEntry("episodic", "c.md", "c", "Third")]
        await organizer._update_index(entries)
        index_content = index_path.read_text()
        assert "a.md" not in index_content
        assert "b.md" in index_content
        assert "c.md" in index_content
        archive = root / "INDEX.archive.md"
        assert archive.exists()
        assert "a.md" in archive.read_text()
