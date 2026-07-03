"""Tests for memory prompt selection (window / hybrid / curator)."""

from __future__ import annotations

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.context.memory_prompt import (
    memory_confidence,
    memory_coverage,
    prepare_memory_for_prompt,
    reset_curation_cache_for_tests,
)
from monkeybot.core.llm.provider import Done, TextDelta
from monkeybot.core.types.types_tools import ToolDef


class _FakeProvider:
    name = "fake"
    supports_streaming = True

    def __init__(self, text: str = '{"memory_line_indices": [1]}') -> None:
        self._text = text

    async def stream(self, messages, tools, *, model: str, thinking_budget=None):
        del messages, tools, model, thinking_budget
        yield TextDelta(text=self._text)
        yield Done()


class _FailingProvider:
    """Simulates a curator call that fails to produce valid JSON."""

    name = "fake-failing"
    supports_streaming = True

    async def stream(self, messages, tools, *, model: str, thinking_budget=None):
        del messages, tools, model, thinking_budget
        yield TextDelta(text="not json")
        yield Done()


def _ctx(memory: list[str]) -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Bot",
        memory_index=memory,
        skills=[],
        tools=[ToolDef("read_file", "read", {})],
        user_id=None,
        parent_run_id=None,
        model="m",
        memory=None,
        context_curation_enabled=True,
    )


def test_memory_coverage_and_confidence() -> None:
    assert memory_coverage(12, 47) == pytest.approx(12 / 47)
    assert memory_confidence(coverage=0.25, truncated=True) == 0.25
    assert memory_confidence(coverage=1.0, truncated=False) == 1.0


@pytest.mark.asyncio
async def test_window_mode_truncates_with_nudge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTEXT_CURATION_ENABLED", "1")
    monkeypatch.setenv("CONTEXT_CURATION_MODE", "window")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_THRESHOLD", "2")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_WINDOW_LINES", "2")
    lines = [f"entry-{i}" for i in range(5)]
    sel = await prepare_memory_for_prompt(
        ctx=_ctx(lines),
        user_message="hello",
        provider=_FakeProvider(),
        curator_provider=None,
    )
    assert sel.lines == ["entry-3", "entry-4"]
    assert sel.total_lines == 5
    assert sel.nudge_search is True
    assert sel.confidence < 1.0


@pytest.mark.asyncio
async def test_curator_cache_skips_second_call(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_curation_cache_for_tests()
    monkeypatch.setenv("CONTEXT_CURATION_ENABLED", "1")
    monkeypatch.setenv("CONTEXT_CURATION_MODE", "curator")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_THRESHOLD", "0")
    prov = _FakeProvider('{"memory_line_indices": [1]}')
    ctx = _ctx(["alpha", "beta"])
    sel1 = await prepare_memory_for_prompt(
        ctx=ctx, user_message="x", provider=prov, curator_provider=None
    )
    sel2 = await prepare_memory_for_prompt(
        ctx=ctx, user_message="y", provider=prov, curator_provider=None
    )
    assert sel1.lines == ["alpha"]
    assert sel2.lines == ["alpha"]


@pytest.mark.asyncio
async def test_curator_mode_falls_back_to_window_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curator-mode failures must fail open to the bounded window, not the full index."""
    reset_curation_cache_for_tests()
    monkeypatch.setenv("CONTEXT_CURATION_ENABLED", "1")
    monkeypatch.setenv("CONTEXT_CURATION_MODE", "curator")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_THRESHOLD", "2")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_WINDOW_LINES", "2")
    lines = [f"entry-{i}" for i in range(5)]
    sel = await prepare_memory_for_prompt(
        ctx=_ctx(lines),
        user_message="hello",
        provider=_FailingProvider(),
        curator_provider=None,
    )
    assert sel.lines == ["entry-3", "entry-4"]
    assert sel.total_lines == 5
    assert sel.nudge_search is True
