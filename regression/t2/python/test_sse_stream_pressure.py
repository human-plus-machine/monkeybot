"""T2-GW-06, T2-GW-07, and T2-GW-21 against the in-process session bus."""

from __future__ import annotations

import asyncio
import json

import pytest

from regression.t2.gateway.harness import data_ids, listening, payload, wait_turn
from testkit.gateway import GatewaySession, open_gateway

_HELLO = """\
- - type: text
    text: hello
  - type: done
"""


def _bus(gateway: GatewaySession, session_id: str):
    return gateway.app.state.registry.get(session_id)


@pytest.mark.case("T2-GW-06")
async def test_replay_overflow_does_not_invent_ids() -> None:
    """Past SSE_REPLAY_MAX, a reconnect replays surviving ids and skips the gap."""
    async with open_gateway(script=_HELLO, env={"SSE_REPLAY_MAX": "2"}) as gateway:
        await gateway.create_session("pressure-06")
        async with listening(gateway, "pressure-06") as reader:
            await reader.until("ActiveRequests")
            response = await gateway.post_reply("pressure-06", "r1", "Say hello")
            assert response.status_code == 200
            live = await reader.until("TurnComplete")
        original = data_ids(live)
        assert len(original) > 2
        async with listening(gateway, "pressure-06", last_event_id="1") as reader:
            replayed: list[str] = []
            saw_active = False
            while True:
                frame = payload(await reader.next_frame())
                if frame.get("type") == "ActiveRequests":
                    saw_active = True
                    assert "id" not in frame
                    break
                raw = frame.get("id")
                assert isinstance(raw, str) and raw
                replayed.append(raw)
        assert saw_active
        assert replayed
        assert all(int(item) > 1 for item in replayed)
        assert set(replayed) <= set(original)
        assert set(original) - set(replayed)
        assert replayed != [str(index) for index in range(1, len(replayed) + 1)]
        await wait_turn(gateway, "pressure-06")


@pytest.mark.case("T2-GW-07")
async def test_nested_lane_flood_keeps_parent_turn_complete() -> None:
    """More than 256 nested frames do not drop the parent TurnComplete from replay."""
    async with open_gateway(script=_HELLO) as gateway:
        await gateway.create_session("pressure-07")
        bus = _bus(gateway, "pressure-07")
        assert bus is not None
        last_nested = 0
        for index in range(300):
            last_nested = await bus.publish_data(
                json.dumps({"type": "NestedProgress", "n": index}),
                lane="nested",
            )
        async with listening(gateway, "pressure-07") as reader:
            await reader.until("ActiveRequests")
            response = await gateway.post_reply("pressure-07", "parent-1", "Say hello")
            assert response.status_code == 200
            live = await reader.until("TurnComplete")
        parent_ids = data_ids(live)
        complete_id = next(frame["id"] for frame in live if frame.get("type") == "TurnComplete")
        async with listening(gateway, "pressure-07", last_event_id=str(last_nested)) as reader:
            replayed = data_ids(await reader.until("TurnComplete"))
        assert complete_id in replayed
        assert int(complete_id) > last_nested
        assert set(replayed) <= set(parent_ids) | {complete_id}
        assert not any(int(item) <= last_nested for item in replayed)
        await wait_turn(gateway, "pressure-07")


@pytest.mark.case("T2-GW-21")
async def test_slow_subscriber_does_not_block_the_producer() -> None:
    """A subscriber that never reads still lets the turn finish, and its queue grows."""
    async with open_gateway(script=_HELLO) as gateway:
        await gateway.create_session("pressure-21")
        bus = _bus(gateway, "pressure-21")
        assert bus is not None
        _replay, queue = await bus.subscribe(None)
        try:
            response = await gateway.post_reply("pressure-21", "r1", "Say hello")
            assert response.status_code == 200
            await asyncio.wait_for(wait_turn(gateway, "pressure-21"), timeout=20)
            assert queue.qsize() > 0
        finally:
            await bus.unsubscribe(queue)
