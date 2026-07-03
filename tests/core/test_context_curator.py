"""Tests for :mod:`monkeybot.core.context.curator`."""

from __future__ import annotations

import pytest

from monkeybot.core.context import SkillRef, TurnContext
from monkeybot.core.context.curator import (
    CuratedPromptParts,
    curation_prompt_injection,
    curation_threshold_met,
    memory_index_token_estimate,
    run_context_curator,
)
from monkeybot.core.llm.provider import Done, TextDelta
from monkeybot.core.types.types_tools import ToolDef


class _FakeCuratorProvider:
    def __init__(self, text: str) -> None:
        self._text = text
        self.stream_calls = 0

    @property
    def name(self) -> str:
        return "fake-curator"

    @property
    def supports_streaming(self) -> bool:
        return True

    async def stream(self, messages, tools, *, model: str, thinking_budget=None):
        del messages, tools, model
        self.stream_calls += 1
        yield TextDelta(text=self._text)
        yield Done()


def _ctx(*, memory: list[str], skills: list[SkillRef] | None = None) -> TurnContext:
    return TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="# Bot",
        memory_index=memory,
        skills=skills or [],
        tools=[ToolDef("read_file", "read", {})],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
        memory=None,
        context_curation_enabled=True,
    )


@pytest.mark.asyncio
async def test_curator_accepts_index_selection() -> None:
    prov = _FakeCuratorProvider('{"memory_line_indices": [1]}')
    ctx = _ctx(memory=["alpha note", "beta note"])
    out = await run_context_curator(
        ctx=ctx,
        provider=prov,
        curator_model="gemini-2.5-flash",
        user_message="about alpha",
    )
    assert isinstance(out, CuratedPromptParts)
    assert out.success
    assert out.memory_lines == ["alpha note"]


@pytest.mark.asyncio
async def test_curator_partial_indices_succeed() -> None:
    prov = _FakeCuratorProvider('{"memory_line_indices": [1, 99]}')
    ctx = _ctx(memory=["alpha note", "beta note"])
    out = await run_context_curator(
        ctx=ctx,
        provider=prov,
        curator_model="m",
        user_message="x",
    )
    assert out.success
    assert out.memory_lines == ["alpha note"]


@pytest.mark.asyncio
async def test_curator_invalid_indices_fail() -> None:
    prov = _FakeCuratorProvider('{"memory_line_indices": [99]}')
    ctx = _ctx(memory=["real"])
    out = await run_context_curator(
        ctx=ctx,
        provider=prov,
        curator_model="m",
        user_message="x",
    )
    assert not out.success
    assert out.memory_lines == []


@pytest.mark.asyncio
async def test_curator_bad_json_fails() -> None:
    prov = _FakeCuratorProvider("not json")
    ctx = _ctx(memory=[])
    out = await run_context_curator(ctx=ctx, provider=prov, curator_model="m", user_message="")
    assert not out.success


def test_curation_prompt_injection_fail_open() -> None:
    use_curated, lines = curation_prompt_injection(CuratedPromptParts([], success=False))
    assert use_curated is False
    assert lines == []

    use_curated, lines = curation_prompt_injection(
        CuratedPromptParts(["a"], success=True),
    )
    assert use_curated is True
    assert lines == ["a"]


def test_curation_threshold_ignores_skill_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_THRESHOLD", "8")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_TOKEN_THRESHOLD", "2000")
    many_skills = [SkillRef(name=f"s{i}", description="d") for i in range(20)]
    ctx = _ctx(memory=["m1", "m2"], skills=many_skills)
    assert not curation_threshold_met(ctx)

    ctx_large_mem = _ctx(memory=[f"m{i}" for i in range(10)], skills=many_skills)
    assert curation_threshold_met(ctx_large_mem)


def test_curation_threshold_by_token_estimate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_THRESHOLD", "100")
    monkeypatch.setenv("CONTEXT_CURATION_MEMORY_TOKEN_THRESHOLD", "50")
    long_line = "x" * 400
    ctx = _ctx(memory=[long_line])
    assert memory_index_token_estimate([long_line]) > 50
    assert curation_threshold_met(ctx)
