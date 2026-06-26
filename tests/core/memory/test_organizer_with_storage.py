"""MemoryOrganizer against :class:`tests.core.memory.fake_workspace_storage.FakeWorkspaceStorage`."""

from __future__ import annotations

import pytest

from monkeybot.core.llm.provider import Done, TextDelta, UsageEvent
from monkeybot.core.memory.organizer import MemoryOrganizer
from monkeybot.core.memory.storage_ops import INDEX_FILENAME
from monkeybot.core.testing.mocks_provider import fake_provider_prompt_tokens
from tests.core.memory.fake_workspace_storage import FakeWorkspaceStorage


class FakeProvider:
    name = "fake"
    supports_streaming = True

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def stream(self, messages, tools, *, model: str, thinking_budget=None):
        del messages, tools, model
        if not self._responses:
            raise RuntimeError("LLM unavailable")
        text = self._responses.pop(0)
        yield TextDelta(text=text)
        yield UsageEvent(input_tokens=1, output_tokens=1, cached_tokens=0)
        yield Done()

    async def count_input_tokens(self, messages, tools, *, model: str):
        del model
        return fake_provider_prompt_tokens(messages, tools)


@pytest.mark.asyncio
async def test_run_returns_zeros_when_raw_empty() -> None:
    st = FakeWorkspaceStorage()
    org = MemoryOrganizer(provider=FakeProvider([]), model="m", storage=st)
    result = await org.run()
    assert result.files_processed == 0
    assert result.files_written == 0
    assert result.index_updated is False


@pytest.mark.asyncio
async def test_run_processes_raw_moves_to_processed_updates_index() -> None:
    st = FakeWorkspaceStorage()
    st.files["raw/2026-note.md"] = "Observation body."
    org = MemoryOrganizer(
        provider=FakeProvider(
            [
                "Summary text.",
                "semantic",
                "tags: t\nsummary: one line",
            ],
        ),
        model="m",
        storage=st,
    )
    result = await org.run()
    assert result.files_processed == 1
    assert result.files_written == 1
    assert result.index_updated is True
    assert "raw/2026-note.md" not in st.files
    assert any(k.startswith("raw/processed/") for k in st.files)
    assert any(k.startswith("semantic/") for k in st.files)
    assert INDEX_FILENAME in st.files


@pytest.mark.asyncio
async def test_run_skips_files_already_under_raw_processed() -> None:
    st = FakeWorkspaceStorage()
    st.files["raw/keep.md"] = "process me"
    st.files["raw/processed/old.md"] = "already done"
    org = MemoryOrganizer(
        provider=FakeProvider(
            [
                "Summary.",
                "episodic",
                "tags: x\nsummary: y",
            ],
        ),
        model="m",
        storage=st,
    )
    await org.run()
    assert "raw/processed/old.md" in st.files
    assert "raw/keep.md" not in st.files
