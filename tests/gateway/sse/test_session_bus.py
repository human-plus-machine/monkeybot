"""Unit tests for SessionBus replay, heartbeats, and SSE helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from monkeybot.core.persistence.transcript import TranscriptWriter
from monkeybot.core.runtime.events import Thinking
from monkeybot.gateway.sse.session_bus import SessionBus, SessionRegistry
from monkeybot.gateway.sse.sse import agent_event_to_wire_dict, format_active_requests


@pytest.mark.asyncio
async def test_subscribe_replays_frames_after_last_event_id() -> None:
    bus = SessionBus(created_at_ms=0, agent_md=None)
    await bus.publish_data('{"seq_hint":1}')
    await bus.publish_data('{"seq_hint":2}')
    await bus.publish_data('{"seq_hint":3}')
    replay, _q = await bus.subscribe(1)
    assert len(replay) == 2
    assert '"seq_hint":2' in replay[0]
    assert '"seq_hint":3' in replay[1]


@pytest.mark.asyncio
async def test_replay_buffer_caps_at_maxlen() -> None:
    bus = SessionBus(created_at_ms=0, agent_md=None, replay_maxlen=2)
    for i in range(5):
        await bus.publish_data(f'{{"i":{i}}}')
    replay, _q = await bus.subscribe(0)
    assert len(replay) == 2
    assert '"i":3' in replay[0]
    assert '"i":4' in replay[1]


@pytest.mark.asyncio
async def test_nested_replay_lane_does_not_evict_primary() -> None:
    bus = SessionBus(
        created_at_ms=0,
        agent_md=None,
        replay_maxlen=2,
        nested_replay_maxlen=2,
    )
    await bus.publish_data('{"lane":"primary","i":1}', lane="primary")
    await bus.publish_data('{"lane":"primary","i":2}', lane="primary")
    for i in range(5):
        await bus.publish_data(f'{{"lane":"nested","i":{i}}}', lane="nested")
    replay, _q = await bus.subscribe(0)
    # Both primary frames survive despite nested overflow.
    assert any('"lane":"primary","i":1' in frame for frame in replay)
    assert any('"lane":"primary","i":2' in frame for frame in replay)
    # Nested lane keeps only its last two.
    nested_frames = [f for f in replay if '"lane":"nested"' in f]
    assert len(nested_frames) == 2
    assert '"i":3' in nested_frames[0]
    assert '"i":4' in nested_frames[1]
    seqs = []
    for frame in replay:
        for line in frame.splitlines():
            if line.startswith("id:"):
                seqs.append(int(line[len("id:") :].strip()))
    assert seqs == sorted(seqs)


@pytest.mark.asyncio
async def test_ping_frames_not_added_to_replay() -> None:
    bus = SessionBus(created_at_ms=0, agent_md=None)
    await bus.publish_data('{"one":true}')
    await bus.publish_comment(": ping 1\n\n")
    await bus.publish_comment(": ping 2\n\n")
    replay, _q = await bus.subscribe(0)
    assert len(replay) == 1
    assert "ping" not in replay[0]


@pytest.mark.asyncio
async def test_live_queue_receives_after_replay() -> None:
    bus = SessionBus(created_at_ms=0, agent_md=None)
    replay, q = await bus.subscribe(None)
    assert replay == []
    await bus.publish_data('{"live":1}')
    frame = await asyncio.wait_for(q.get(), timeout=2.0)
    assert "data:" in frame


def test_agent_event_dict_maps_kind_to_type() -> None:
    out = agent_event_to_wire_dict(Thinking(request_id="u1"))
    assert out["type"] == "Thinking"
    assert out["request_id"] == "u1"
    assert out["chat_request_id"] == "u1"


def test_active_requests_frame_has_no_id_line() -> None:
    frame = format_active_requests(["a"])
    assert frame.startswith("data:")
    assert not frame.startswith("id:")


def test_registry_remove_drops_session_and_returns_true() -> None:
    reg = SessionRegistry()
    reg.create("s1", agent_md=None, created_at_ms=0)
    assert reg.get("s1") is not None

    result = reg.remove("s1")
    assert result.deleted is True
    assert result.transcript_dir is None
    assert reg.get("s1") is None


def test_registry_remove_unknown_session_returns_false() -> None:
    reg = SessionRegistry()
    assert reg.remove("nope").deleted is False


@pytest.mark.asyncio
async def test_registry_remove_cleans_spill_files(tmp_path: Path) -> None:
    """Session end removes parent and subagent spill dirs under the session namespace."""
    spill = tmp_path / ".monkeybot" / "spill" / "s-spill"
    spill.mkdir(parents=True)
    (spill / "call.txt").write_text("payload", encoding="utf-8")
    sub_spill = tmp_path / ".monkeybot" / "spill" / "subagent:s-spill:abc123"
    sub_spill.mkdir(parents=True)
    (sub_spill / "tool.txt").write_text("sub", encoding="utf-8")
    other = tmp_path / ".monkeybot" / "spill" / "other-session"
    other.mkdir(parents=True)
    (other / "keep.txt").write_text("keep", encoding="utf-8")
    other_sub = tmp_path / ".monkeybot" / "spill" / "subagent:other-session:xyz"
    other_sub.mkdir(parents=True)
    (other_sub / "keep.txt").write_text("keep", encoding="utf-8")

    reg = SessionRegistry(workspace_root=tmp_path)
    reg.create("s-spill", agent_md=None, created_at_ms=0)
    assert (await reg.remove_async("s-spill")).deleted is True

    assert not spill.exists()
    assert not sub_spill.exists()
    assert (other / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (other_sub / "keep.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_registry_sync_remove_defers_spill_cleanup(tmp_path: Path) -> None:
    """Sync remove detaches immediately; spill cleanup runs only on remove_async."""
    spill = tmp_path / ".monkeybot" / "spill" / "s-sync"
    spill.mkdir(parents=True)
    (spill / "call.txt").write_text("payload", encoding="utf-8")

    reg = SessionRegistry(workspace_root=tmp_path)
    reg.create("s-sync", agent_md=None, created_at_ms=0)
    assert reg.remove("s-sync").deleted is True
    assert spill.exists()


@pytest.mark.asyncio
async def test_registry_remove_cancels_pending_responses() -> None:
    reg = SessionRegistry()
    bus = reg.create("s1", agent_md=None, created_at_ms=0)
    fut = bus.register_pending("p1")

    reg.remove("s1")

    assert fut.cancelled()


@pytest.mark.asyncio
async def test_registry_remove_async_returns_transcript_dir(tmp_path: Path) -> None:
    reg = SessionRegistry()
    bus = reg.create("s-tx", agent_md=None, created_at_ms=0)
    writer = TranscriptWriter("s-tx", workspace_root=tmp_path)
    await writer.ensure_manifest(model="gpt-test", provider="fake")
    await writer.write_user_message(request_id="r1", content="hi")
    bus.transcript_writer = writer

    result = await reg.remove_async("s-tx")
    assert result.deleted is True
    assert result.transcript_dir is not None
    report_dir = Path(result.transcript_dir)
    assert (report_dir / "transcript.ndjson").is_file()
    assert not (report_dir / "brief.md").exists()
    assert not (report_dir / "report.json").exists()
    assert not (report_dir / "meta.json").exists()


@pytest.mark.asyncio
async def test_registry_remove_async_awaits_active_turn_before_returning(
    tmp_path: Path,
) -> None:
    """DELETE during a running turn must wait until the turn finishes writing."""
    import json

    from monkeybot.core.runtime.events import TurnComplete, UsageTotals

    reg = SessionRegistry()
    bus = reg.create("s-race", agent_md=None, created_at_ms=0)
    writer = TranscriptWriter("s-race", workspace_root=tmp_path)
    await writer.ensure_manifest(model="gpt-test", provider="fake")
    await writer.write_user_message(request_id="r1", content="hi")
    bus.transcript_writer = writer
    bus.current_request_id = "r1"

    wrote_late = asyncio.Event()

    async def _slow_turn() -> None:
        # Simulate a turn that still has work after DELETE detaches the session.
        await asyncio.sleep(0.05)
        assert bus.cancel_requested_for == "r1"
        await writer.write_event(
            TurnComplete(request_id="r1", usage=UsageTotals(duration_ms=12))
        )
        bus.current_request_id = None
        wrote_late.set()

    bus.active_turn_task = asyncio.create_task(_slow_turn())

    result = await reg.remove_async("s-race")
    assert result.deleted is True
    assert wrote_late.is_set()
    assert result.transcript_dir is not None

    lines = [
        json.loads(line)
        for line in writer.path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    types = [line.get("type") for line in lines]
    assert "TurnComplete" in types
    assert not (Path(result.transcript_dir) / "report.json").exists()


def test_request_cancel_ignores_stale_request_id() -> None:
    import asyncio

    from monkeybot.gateway.sse.session_bus import SessionBus

    bus = SessionBus(created_at_ms=0, agent_md=None)
    cancel = asyncio.Event()
    bus.turn_cancel_event = cancel
    bus.current_request_id = "rid-current"
    bus.request_cancel("rid-stale")
    assert bus.cancel_requested_for == "rid-stale"
    assert not cancel.is_set()
    bus.request_cancel("rid-current")
    assert cancel.is_set()
