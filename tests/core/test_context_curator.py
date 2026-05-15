"""Tests for :mod:`monkeybot.core.context_curator`."""

from __future__ import annotations

import pytest
from monkeybot.core.context import SkillRef, TurnContext
from monkeybot.core.context_curator import CuratedPromptParts, run_context_curator
from monkeybot.core.provider import Done, TextDelta
from monkeybot.core.types_tools import ToolDef


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

    async def stream(self, messages, tools, *, model: str):
        del messages, tools, model
        self.stream_calls += 1
        yield TextDelta(text=self._text)
        yield Done()


def _ctx(*, memory: list[str], skills: list[SkillRef]) -> TurnContext:
    return TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="# Bot",
        memory_index=memory,
        skills=skills,
        tools=[ToolDef("read_file", "read", {})],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
        memory_path=None,
        context_curation_enabled=True,
    )


@pytest.mark.asyncio
async def test_curator_accepts_verbatim_memory_and_skills() -> None:
    prov = _FakeCuratorProvider(
        '{"memory_lines": ["alpha note"], "highlighted_skills": ["skill-a"]}',
    )
    skills = [
        SkillRef(name="skill-a", description="A", entry_point="skill-a/run.py"),
        SkillRef(name="skill-b", description="B", entry_point="skill-b/run.py"),
    ]
    ctx = _ctx(memory=["alpha note", "beta note"], skills=skills)
    out = await run_context_curator(
        ctx=ctx,
        provider=prov,
        curator_model="gemini-2.5-flash",
        user_message="about alpha",
    )
    assert isinstance(out, CuratedPromptParts)
    assert out.success
    assert out.memory_lines == ["alpha note"]
    assert len(out.skills) == 1
    assert out.skills[0].name == "skill-a"


@pytest.mark.asyncio
async def test_curator_invalid_selection_fails_empty() -> None:
    prov = _FakeCuratorProvider(
        '{"memory_lines": ["not in index"], "highlighted_skills": ["nope"]}',
    )
    ctx = _ctx(memory=["real"], skills=[SkillRef("skill-a", "A", "skill-a/run.py")])
    out = await run_context_curator(
        ctx=ctx,
        provider=prov,
        curator_model="m",
        user_message="x",
    )
    assert not out.success
    assert out.memory_lines == []
    assert out.skills == []


@pytest.mark.asyncio
async def test_curator_bad_json_fails() -> None:
    prov = _FakeCuratorProvider("not json")
    ctx = _ctx(memory=[], skills=[])
    out = await run_context_curator(ctx=ctx, provider=prov, curator_model="m", user_message="")
    assert not out.success
