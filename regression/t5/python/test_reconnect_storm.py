"""T5-GW-02: twenty reconnects, one TurnComplete each, tool call ids still paired.

In-process gateway and a scripted provider. No agent process and no provider key.
"""

from __future__ import annotations

import pytest

from regression.t2.gateway.harness import listening, payload, wait_turn
from testkit.gateway import open_gateway

pytestmark = [pytest.mark.slow, pytest.mark.case("T5-GW-02")]

_TRIALS = 20
_TOOL = """\
- - type: tool_call
    call_id: c1
    name: todo_list
    args:
      action: add
      text: Write notes
  - type: done
- - type: text
    text: noted
  - type: done
"""


def _of_type(frames: list[dict[str, object]], kind: str) -> list[dict[str, object]]:
    return [frame for frame in frames if frame.get("type") == kind]


def _call_ids(frames: list[dict[str, object]], kind: str) -> set[str]:
    found: set[str] = set()
    for frame in _of_type(frames, kind):
        call_id = frame.get("call_id")
        if isinstance(call_id, str) and call_id:
            found.add(call_id)
    return found


@pytest.mark.asyncio
async def test_reconnect_storm_keeps_one_turn_and_paired_calls() -> None:
    async with open_gateway(script=_TOOL) as gateway:
        for index in range(_TRIALS):
            session_id = f"reconnect-{index}"
            request_id = f"r-{index}"
            await gateway.create_session(session_id)
            async with listening(gateway, session_id) as reader:
                await reader.until("ActiveRequests")
                posted = await gateway.post_reply(session_id, request_id, "add a note")
                assert posted.status_code == 200, posted.text[:300]
                head: dict[str, object] | None = None
                while head is None:
                    body = payload(await reader.next_frame())
                    if isinstance(body.get("id"), str):
                        head = body
            assert head is not None
            last_id = str(head["id"])
            async with listening(gateway, session_id, last_event_id=last_id) as reader:
                tail = await reader.until("TurnComplete")
            combined = [head, *tail]
            assert len(_of_type(combined, "TurnComplete")) == 1
            started = _call_ids(combined, "ToolCallStarted")
            finished = _call_ids(combined, "ToolCallResult")
            assert started
            assert started <= finished
            await wait_turn(gateway, session_id)
