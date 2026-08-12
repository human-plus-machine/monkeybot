"""Tests for memory prompt selection (wake-up pass-through)."""

from __future__ import annotations

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.context.memory_prompt import prepare_memory_for_prompt
from monkeybot.core.types.types_tools import ToolDef


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


@pytest.mark.asyncio
async def test_prepare_memory_passes_through_wake_up_lines() -> None:
    lines = ["## L0 — IDENTITY", "I am test-agent."]
    sel = await prepare_memory_for_prompt(_ctx(lines))
    assert sel.lines == lines
