"""Tests for :mod:`monkeybot.core.context.curator`."""

from __future__ import annotations

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.context.curator import (
    CuratedPromptParts,
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


def _ctx(*, memory: list[str]) -> TurnContext:
    return TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="# Bot",
        memory_index=memory,
        skills=[],
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
        max_memory_lines=12,
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
        max_memory_lines=12,
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
        max_memory_lines=12,
    )
    assert not out.success
    assert out.memory_lines == []


@pytest.mark.asyncio
async def test_curator_bad_json_fails() -> None:
    prov = _FakeCuratorProvider("not json")
    ctx = _ctx(memory=[])
    out = await run_context_curator(
        ctx=ctx, provider=prov, curator_model="m", user_message="", max_memory_lines=12
    )
    assert not out.success
