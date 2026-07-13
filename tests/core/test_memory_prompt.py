"""Tests for memory prompt selection (window + token-heavy curator)."""

from __future__ import annotations

import pytest

from monkeybot.core.context import SkillRef, TurnContext
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
        self.vertex_google_search_calls: list[bool] = []
        self.stream_calls = 0

    async def stream(
        self, messages, tools, *, model: str, thinking_budget=None, vertex_google_search=False
    ):
        del messages, tools, model, thinking_budget
        self.stream_calls += 1
        self.vertex_google_search_calls.append(vertex_google_search)
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


def _ctx(memory: list[str], *, skills: list[SkillRef] | None = None) -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Bot",
        memory_index=memory,
        skills=skills or [],
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
async def test_window_truncates_when_index_exceeds_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below the token threshold, curation uses the recent window only."""
    monkeypatch.setenv("CONTEXT_CURATION_ENABLED", "1")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_WINDOW_LINES", "2")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_TOKEN_THRESHOLD", "2000")
    lines = [f"entry-{i}" for i in range(5)]
    prov = _FakeProvider()
    sel = await prepare_memory_for_prompt(
        ctx=_ctx(lines),
        user_message="hello",
        provider=prov,
        curator_provider=None,
    )
    assert sel.lines == ["entry-3", "entry-4"]
    assert sel.total_lines == 5
    assert sel.nudge_search is True
    assert sel.confidence < 1.0
    assert prov.stream_calls == 0


@pytest.mark.asyncio
async def test_curator_cache_skips_second_call_same_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_curation_cache_for_tests()
    monkeypatch.setenv("CONTEXT_CURATION_ENABLED", "1")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_WINDOW_LINES", "2")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_TOKEN_THRESHOLD", "1")
    prov = _FakeProvider('{"memory_line_indices": [1]}')
    ctx = _ctx(["alpha", "beta"])
    sel1 = await prepare_memory_for_prompt(
        ctx=ctx, user_message="x", provider=prov, curator_provider=None
    )
    sel2 = await prepare_memory_for_prompt(
        ctx=ctx, user_message="x", provider=prov, curator_provider=None
    )
    assert sel1.lines == ["alpha"]
    assert sel2.lines == ["alpha"]
    assert prov.stream_calls == 1


@pytest.mark.asyncio
async def test_curator_cache_misses_on_different_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache key includes the user message; different queries must re-run the curator."""
    reset_curation_cache_for_tests()
    monkeypatch.setenv("CONTEXT_CURATION_ENABLED", "1")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_WINDOW_LINES", "2")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_TOKEN_THRESHOLD", "1")
    prov = _FakeProvider('{"memory_line_indices": [1]}')
    ctx = _ctx(["alpha", "beta"])
    await prepare_memory_for_prompt(
        ctx=ctx, user_message="x", provider=prov, curator_provider=None
    )
    await prepare_memory_for_prompt(
        ctx=ctx, user_message="y", provider=prov, curator_provider=None
    )
    assert prov.stream_calls == 2


@pytest.mark.asyncio
async def test_full_index_when_within_window_and_token_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTEXT_CURATION_ENABLED", "1")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_WINDOW_LINES", "12")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_TOKEN_THRESHOLD", "2000")
    lines = [f"entry-{i}" for i in range(12)]
    prov = _FakeProvider()
    sel = await prepare_memory_for_prompt(
        ctx=_ctx(lines),
        user_message="hello",
        provider=prov,
        curator_provider=None,
    )
    assert sel.lines == lines
    assert sel.nudge_search is False
    assert prov.stream_calls == 0


@pytest.mark.asyncio
async def test_skill_count_does_not_trigger_curation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTEXT_CURATION_ENABLED", "1")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_WINDOW_LINES", "8")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_TOKEN_THRESHOLD", "2000")
    many_skills = [SkillRef(name=f"s{i}", description="d") for i in range(20)]
    prov = _FakeProvider()
    sel = await prepare_memory_for_prompt(
        ctx=_ctx(["m1", "m2"], skills=many_skills),
        user_message="hello",
        provider=prov,
        curator_provider=None,
    )
    assert sel.lines == ["m1", "m2"]
    assert prov.stream_calls == 0


@pytest.mark.asyncio
async def test_curator_call_never_receives_vertex_google_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The curator's own LLM call is an internal harness call, not an agent turn.

    Regression guard: it must never be able to receive ``vertex_google_search=True``,
    even though nothing here threads the flag in explicitly — pins the invariant so a
    future refactor can't accidentally wire it through.
    """
    reset_curation_cache_for_tests()
    monkeypatch.setenv("CONTEXT_CURATION_ENABLED", "1")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_WINDOW_LINES", "2")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_TOKEN_THRESHOLD", "1")
    prov = _FakeProvider('{"memory_line_indices": [1]}')
    ctx = _ctx(["alpha", "beta"])
    await prepare_memory_for_prompt(
        ctx=ctx, user_message="x", provider=prov, curator_provider=None
    )
    assert prov.vertex_google_search_calls == [False]


@pytest.mark.asyncio
async def test_token_heavy_runs_curator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token-heavy indexes must invoke the curator rather than silently using the window."""
    reset_curation_cache_for_tests()
    monkeypatch.setenv("CONTEXT_CURATION_ENABLED", "1")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_TOKEN_THRESHOLD", "1")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_WINDOW_LINES", "2")
    lines = [f"entry-{i}" for i in range(5)]
    prov = _FakeProvider('{"memory_line_indices": [1, 2]}')
    sel = await prepare_memory_for_prompt(
        ctx=_ctx(lines), user_message="hello", provider=prov, curator_provider=None
    )
    # Curator picked lines 1 and 2 (entry-0, entry-1); the window fallback would
    # instead have picked the most recent tail (entry-3, entry-4).
    assert sel.lines == ["entry-0", "entry-1"]
    assert sel.use_custom_lines is True
    assert prov.stream_calls == 1
    assert prov.vertex_google_search_calls == [False]


@pytest.mark.asyncio
async def test_curator_falls_back_to_window_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curator failures must fail open to the bounded window, not the full index."""
    reset_curation_cache_for_tests()
    monkeypatch.setenv("CONTEXT_CURATION_ENABLED", "1")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_WINDOW_LINES", "2")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_TOKEN_THRESHOLD", "1")
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
