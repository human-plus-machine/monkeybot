"""Unit tests for SessionBus replay, heartbeats, and SSE helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from monkeybot.core.context.memory_prompt import _curation_cache, reset_curation_cache_for_tests
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
    assert result.transcript_report_dir is None
    assert reg.get("s1") is None


def test_registry_remove_unknown_session_returns_false() -> None:
    reg = SessionRegistry()
    assert reg.remove("nope").deleted is False


def test_registry_remove_evicts_curation_cache_entry() -> None:
    """SessionRegistry.remove must also clear memory_prompt._curation_cache.

    Otherwise per-thread curator selections outlive their session for the
    life of the process (unbounded growth in a long-running gateway).
    """
    reset_curation_cache_for_tests()
    reg = SessionRegistry()
    reg.create("s1", agent_md=None, created_at_ms=0)
    _curation_cache["s1"] = ("fingerprint", ["cached line"])

    reg.remove("s1")

    assert "s1" not in _curation_cache


def test_registry_remove_cleans_spill_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session end removes ``.monkeybot/spill/{session_id}/`` so spills last the session."""
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_ROOT", str(tmp_path))
    spill = tmp_path / ".monkeybot" / "spill" / "s-spill"
    spill.mkdir(parents=True)
    (spill / "call.txt").write_text("payload", encoding="utf-8")
    other = tmp_path / ".monkeybot" / "spill" / "other-session"
    other.mkdir(parents=True)
    (other / "keep.txt").write_text("keep", encoding="utf-8")

    reg = SessionRegistry()
    reg.create("s-spill", agent_md=None, created_at_ms=0)
    assert reg.remove("s-spill").deleted is True

    assert not spill.exists()
    assert (other / "keep.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_registry_remove_cancels_pending_responses() -> None:
    reg = SessionRegistry()
    bus = reg.create("s1", agent_md=None, created_at_ms=0)
    fut = bus.register_pending("p1")

    reg.remove("s1")

    assert fut.cancelled()


@pytest.mark.asyncio
async def test_registry_remove_async_analyzes_transcript(tmp_path: Path) -> None:
    reg = SessionRegistry()
    bus = reg.create("s-tx", agent_md=None, created_at_ms=0)
    writer = TranscriptWriter("s-tx", workspace_root=tmp_path)
    await writer.ensure_manifest(model="gpt-test", provider="fake")
    await writer.write_user_message(request_id="r1", content="hi")
    bus.transcript_writer = writer

    result = await reg.remove_async("s-tx")
    assert result.deleted is True
    assert result.transcript_report_dir is not None
    report_dir = Path(result.transcript_report_dir)
    assert (report_dir / "brief.md").is_file()
    assert (report_dir / "report.json").is_file()
    assert (report_dir / "meta.json").is_file()


@pytest.mark.asyncio
async def test_registry_remove_async_awaits_active_turn_before_analysis(
    tmp_path: Path,
) -> None:
    """DELETE during a running turn must not analyze until the turn finishes writing."""
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
    assert result.transcript_report_dir is not None

    lines = [
        json.loads(line)
        for line in writer.path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    types = [line.get("type") for line in lines]
    assert "TurnComplete" in types
    report = json.loads((Path(result.transcript_report_dir) / "report.json").read_text())
    # Analysis ran after the late TurnComplete, so the turn is closed cleanly.
    assert report["scorecard"]["turn_count"] >= 1
