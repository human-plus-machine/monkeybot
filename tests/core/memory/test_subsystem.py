"""Tests for :class:`~monkeybot.core.memory.subsystem.MemorySubsystem`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import Done, TextDelta, UsageEvent
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from tests.core.memory.fake_workspace_storage import FakeWorkspaceStorage


def _fake_subsystem(storage: FakeWorkspaceStorage, *, uri: str = "fake://mem") -> MemorySubsystem:
    fake = ScriptedFakeProvider(
        [TextDelta(text="x"), UsageEvent(input_tokens=1, output_tokens=1, cached_tokens=0), Done()]
    )
    return MemorySubsystem(storage=storage, provider=fake, model="m", memory_uri=uri)


def _ctx() -> TurnContext:
    return TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="a",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
    )


@pytest.mark.asyncio
async def test_load_index_empty_when_no_index() -> None:
    st = FakeWorkspaceStorage()
    sub = _fake_subsystem(st)
    assert await sub.load_index() == []


@pytest.mark.asyncio
async def test_load_index_parses_lines() -> None:
    st = FakeWorkspaceStorage()
    st.files["INDEX.md"] = "one\ntwo\n"
    sub = _fake_subsystem(st)
    assert await sub.load_index() == ["one", "two"]


@pytest.mark.asyncio
async def test_search_files_finds_hits_skipping_raw_when_skip_raw() -> None:
    st = FakeWorkspaceStorage()
    st.files["semantic/note.md"] = "hello world"
    st.files["raw/x.md"] = "hello raw"
    sub = _fake_subsystem(st)
    out = await sub.search_files("hello", max_hits=10, skip_raw=True)
    hits = out.get("hits", [])
    paths = [h.get("path") for h in hits]
    assert any(p and "semantic" in p for p in paths)
    assert not any(p and p.startswith("raw/") for p in paths)


@pytest.mark.asyncio
async def test_promote_writes_semantic_and_unlinks_source(tmp_path: Path) -> None:
    st = FakeWorkspaceStorage()
    sub = _fake_subsystem(st)
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    src = run_dir / "note.md"
    src.write_text("body", encoding="utf-8")
    await sub.promote("run1", src)
    assert not src.exists()
    assert "semantic/note.md" in st.files
    text = st.files["semantic/note.md"]
    assert "status: active" in text
    assert "body" in text
    assert text.strip().endswith("body")


@pytest.mark.asyncio
async def test_gc_processed_delegates_to_storage_gc_prefix() -> None:
    st = FakeWorkspaceStorage()
    sub = _fake_subsystem(st)
    await sub.gc_processed(max_age_sec=123.0)
    assert st.gc_calls == [("raw/processed/", 123.0)]


@pytest.mark.asyncio
async def test_rebuild_graph_indexes_legacy_notes_without_frontmatter(
    tmp_path: Path,
) -> None:
    storage = FakeWorkspaceStorage()
    storage.files["episodic/old.md"] = "Remembered a meeting about cats.\n"
    storage.files["semantic/prefs.md"] = (
        "---\ntype: semantic\nstatus: active\n---\n\nPrefers dark mode.\n"
    )
    sub = _fake_subsystem(storage, uri=f"local://{tmp_path / 'mem'}")
    try:
        stats = await sub.rebuild_graph()
        assert stats["scanned"] == 2
        assert stats["upserted"] == 2
        graph = await sub.export_graph()
        paths = {n["path"] for n in graph["nodes"]}
        assert paths == {"episodic/old.md", "semantic/prefs.md"}
        by_path = {n["path"]: n for n in graph["nodes"]}
        assert by_path["episodic/old.md"]["type"] == "episodic"
        assert by_path["episodic/old.md"]["status"] == "active"
        assert by_path["semantic/prefs.md"]["type"] == "semantic"
    finally:
        await sub.close()


@pytest.mark.asyncio
async def test_remote_memory_disables_graph() -> None:
    st = FakeWorkspaceStorage()
    sub = _fake_subsystem(st, uri="gcs://bucket/mem")
    assert await sub.ensure_graph() is None
    payload = await sub.export_graph()
    assert payload["nodes"] == []
    assert payload["edges"] == []
    assert "local://" in str(payload.get("note") or "")
    stats = await sub.rebuild_graph()
    assert stats.get("skipped") == 1
    await sub.close()


@pytest.mark.asyncio
async def test_register_hooks_wires_memory_hook_events() -> None:
    st = FakeWorkspaceStorage()
    sub = _fake_subsystem(st)
    mgr = HookManager()
    sub.register_hooks(mgr)
    assert HookEvent.USER_MESSAGE in mgr._handlers
    assert mgr._handlers[HookEvent.USER_MESSAGE]

    payload = HookPayload(
        event=HookEvent.USER_MESSAGE,
        thread_id="t",
        request_id="r",
        ctx=_ctx(),
        user_message="hi there",
    )
    await mgr.fire(payload, timeout_s=5.0)
    assert any("chat_log.md" in k for k in st.files)


@pytest.mark.asyncio
async def test_flush_delegates_to_hook() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_organizer() -> None:
        started.set()
        await release.wait()

    st = FakeWorkspaceStorage()
    sub = MemorySubsystem(
        storage=st,
        provider=ScriptedFakeProvider(
            [TextDelta(text="x"), UsageEvent(input_tokens=1, output_tokens=1, cached_tokens=0), Done()]
        ),
        model="m",
        memory_uri="fake://mem",
    )
    # Replace organizer runner on the internal hook (same pattern as test_post_turn_schedules...).
    sub._hook._organizer_runner = fake_organizer  # type: ignore[assignment]

    await sub._hook.on_post_turn(  # noqa: SLF001 — exercise hook path used by POST_TURN
        HookPayload(
            event=HookEvent.POST_TURN,
            thread_id="t",
            request_id="r",
            ctx=_ctx(),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)

    flushed = asyncio.Event()
    entering_flush = asyncio.Event()

    async def do_flush() -> None:
        entering_flush.set()
        await sub.flush()
        flushed.set()

    t = asyncio.create_task(do_flush())
    await asyncio.wait_for(entering_flush.wait(), timeout=1.0)
    assert not flushed.is_set()
    release.set()
    await asyncio.wait_for(flushed.wait(), timeout=1.0)
    await t
