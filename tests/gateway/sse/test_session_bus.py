"""Unit tests for SessionBus replay, heartbeats, and SSE helpers."""

from __future__ import annotations

import asyncio

import pytest

from monkeybot.gateway.sse.session_bus import SessionBus
from monkeybot.gateway.sse.sse import agent_event_to_wire_dict, format_active_requests
from monkeybot.core.runtime.events import Thinking


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

