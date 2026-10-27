"""T5-GW-01: twenty cancels, each with one terminal event and the lock released.

In-process gateway and a scripted provider. No agent process and no provider key.
"""

from __future__ import annotations

import pytest

from regression.t2.gateway.harness import listening, wait_turn
from testkit.gateway import open_gateway

pytestmark = [pytest.mark.slow, pytest.mark.case("T5-GW-01")]

_TRIALS = 20
_CONFIRM = """\
- - type: tool_call
    call_id: loop1
    name: start_loop
    args:
      prompt: ping
      interval: 1h
      max_ticks: 1
  - type: done
"""


def _turn_completes(frames: list[dict[str, object]]) -> list[dict[str, object]]:
    return [frame for frame in frames if frame.get("type") == "TurnComplete"]


def _lock_released(gateway: object, session_id: str) -> bool:
    registry = getattr(getattr(gateway, "app", None).state, "registry", None)
    bus = None if registry is None else registry.get(session_id)
    task = None if bus is None else bus.active_turn_task
    return task is None or task.done()


@pytest.mark.asyncio
async def test_cancel_storm_releases_each_turn() -> None:
    async with open_gateway(script=_CONFIRM) as gateway:
        for index in range(_TRIALS):
            session_id = f"cancel-{index}"
            request_id = f"r-{index}"
            await gateway.create_session(session_id)
            async with listening(gateway, session_id) as reader:
                await reader.until("ActiveRequests")
                posted = await gateway.post_reply(session_id, request_id, "start it")
                assert posted.status_code == 200, posted.text[:300]
                await reader.until("ToolConfirmationRequest")
                cancelled = await gateway.client.post(
                    f"/sessions/{session_id}/cancel",
                    json={"request_id": request_id},
                )
                assert cancelled.status_code == 200, cancelled.text[:300]
                terminal = await reader.until("TurnComplete")
            assert len(_turn_completes(terminal)) == 1
            await wait_turn(gateway, session_id)
            assert _lock_released(gateway, session_id)
