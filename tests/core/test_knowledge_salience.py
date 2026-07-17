"""Tests for F21 salience hooks: index announcer + search usage nudge."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookPayload
from monkeybot.core.knowledge.salience import (
    IndexAnnouncer,
    SearchUsageNudge,
    format_index_announcement,
)
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex


def _ctx() -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="agent",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
        workspace_root=None,
    )


def _payload(event: HookEvent, *, request_id: str = "r1", **kw) -> HookPayload:
    return HookPayload(
        event=event,
        thread_id="t1",
        request_id=request_id,
        ctx=_ctx(),
        **kw,
    )


def test_format_index_announcement_mentions_search() -> None:
    text = format_index_announcement(120, 900, embeddings=True)
    assert "`search`" in text
    assert "120 files" in text
    assert "900 chunks" in text
    assert "embeddings" in text
    assert "embeddings" not in format_index_announcement(1, 1, embeddings=False)


def test_format_index_announcement_surfaces_degraded_reason() -> None:
    """H2: embeddings requested-but-disabled must be visible, not just server logs."""
    text = format_index_announcement(
        5,
        10,
        embeddings=False,
        embeddings_degraded_reason="NVIDIA_API_KEY is not set",
    )
    assert "embeddings.enabled" in text
    assert "NVIDIA_API_KEY is not set" in text

    # No degradation note when embeddings were never requested.
    clean = format_index_announcement(5, 10, embeddings=False)
    assert "embeddings.enabled" not in clean


@pytest.mark.asyncio
async def test_announcer_injects_once_per_thread(tmp_path: Path) -> None:
    index = KnowledgeIndex(tmp_path / "index.sqlite")
    await index.open()
    try:
        await index.upsert_file(
            path="a.md",
            source_type="workspace_file",
            content_hash="h",
            mtime=1.0,
            chunks=[],
            links=[],
        )
        announcer = IndexAnnouncer(index, embeddings_enabled=False)
        pre = _payload(HookEvent.PRE_TURN)
        await announcer.on_pre_turn(pre)
        assert pre.inject_text is not None
        assert "Workspace knowledge index" in pre.inject_text

        pre2 = _payload(HookEvent.PRE_TURN)
        await announcer.on_pre_turn(pre2)
        assert pre2.inject_text is None
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_announcer_includes_degraded_reason(tmp_path: Path) -> None:
    """H2: IndexAnnouncer must forward the degraded reason into the injected notice."""
    index = KnowledgeIndex(tmp_path / "index.sqlite")
    await index.open()
    try:
        await index.upsert_file(
            path="a.md",
            source_type="workspace_file",
            content_hash="h",
            mtime=1.0,
            chunks=[],
            links=[],
        )
        announcer = IndexAnnouncer(
            index,
            embeddings_enabled=False,
            embeddings_degraded_reason="provider setup failed (boom)",
        )
        pre = _payload(HookEvent.PRE_TURN)
        await announcer.on_pre_turn(pre)
        assert pre.inject_text is not None
        assert "provider setup failed (boom)" in pre.inject_text
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_announcer_silent_on_empty_index(tmp_path: Path) -> None:
    index = KnowledgeIndex(tmp_path / "index.sqlite")
    await index.open()
    try:
        announcer = IndexAnnouncer(index, embeddings_enabled=False)
        pre = _payload(HookEvent.PRE_TURN)
        await announcer.on_pre_turn(pre)
        assert pre.inject_text is None
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_nudge_fires_after_threshold_without_search() -> None:
    nudge = SearchUsageNudge(threshold=3)
    for tool in ("grep", "glob", "read_file"):
        await nudge.on_post_tool(_payload(HookEvent.POST_TOOL, tool_name=tool))
    pre = _payload(HookEvent.PRE_TOOL, tool_name="grep")
    await nudge.on_pre_tool(pre)
    assert pre.inject_text is not None
    assert "`search`" in pre.inject_text

    # One-shot per turn
    pre2 = _payload(HookEvent.PRE_TOOL, tool_name="grep")
    await nudge.on_pre_tool(pre2)
    assert pre2.inject_text is None


@pytest.mark.asyncio
async def test_nudge_suppressed_when_search_used() -> None:
    nudge = SearchUsageNudge(threshold=2)
    await nudge.on_post_tool(_payload(HookEvent.POST_TOOL, tool_name="search"))
    for tool in ("grep", "glob", "read_file"):
        await nudge.on_post_tool(_payload(HookEvent.POST_TOOL, tool_name=tool))
    pre = _payload(HookEvent.PRE_TOOL, tool_name="grep")
    await nudge.on_pre_tool(pre)
    assert pre.inject_text is None


@pytest.mark.asyncio
async def test_nudge_resets_on_new_request() -> None:
    nudge = SearchUsageNudge(threshold=2)
    for tool in ("grep", "glob"):
        await nudge.on_post_tool(
            _payload(HookEvent.POST_TOOL, request_id="r1", tool_name=tool)
        )
    # New turn: counters reset, no stale nudge
    pre = _payload(HookEvent.PRE_TOOL, request_id="r2", tool_name="grep")
    await nudge.on_pre_tool(pre)
    assert pre.inject_text is None
