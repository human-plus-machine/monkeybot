"""Unit tests for ToolOutputOffloadMW."""

from __future__ import annotations

import pytest

from src.core.harness.event_bus import EventBus
from src.core.harness.events import Principal, VersionTriple
from src.core.harness.middleware.tool_output_offload import ToolOutputOffloadMW


@pytest.mark.asyncio
async def test_small_output_is_not_offloaded() -> None:
    bus = EventBus(include_default_logger=False)
    mw = ToolOutputOffloadMW(threshold_tokens=1000, event_bus=bus)
    out = await mw.maybe_offload(
        "small",
        call_id="c1",
        run_id="r",
        session_id="s",
        principal=Principal(),
        versions=VersionTriple(harness="1", deep_agents="x", model="y"),
    )
    assert out == "small"
    assert not mw.vfs.paths()


@pytest.mark.asyncio
async def test_large_output_is_offloaded() -> None:
    bus = EventBus(include_default_logger=False)
    mw = ToolOutputOffloadMW(threshold_tokens=10, event_bus=bus)
    content = "x" * 500
    out = await mw.maybe_offload(
        content,
        call_id="call-42",
        run_id="r",
        session_id="s",
        principal=Principal(),
        versions=VersionTriple(harness="1", deep_agents="x", model="y"),
    )
    assert "offloaded" in out
    assert "/.emonk/tool_outputs/call-42.txt" in out
    assert mw.vfs.read("/.emonk/tool_outputs/call-42.txt") == content.encode()
