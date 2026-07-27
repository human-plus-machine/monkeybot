"""Integration: SessionBus EventPublisher adapts nested subagent events onto SSE."""

from __future__ import annotations

import pytest

from monkeybot.core.runtime.events import SubagentStarted, event_from_json
from monkeybot.gateway.sse.app import _SessionBusEventPublisher
from monkeybot.gateway.sse.session_bus import SessionBus


@pytest.mark.asyncio
async def test_session_bus_event_publisher_forwards_subagent_started() -> None:
    bus = SessionBus(created_at_ms=0, agent_md=None)
    publisher = _SessionBusEventPublisher(bus)
    evt = SubagentStarted(
        request_id="req-1",
        parent_call_id="call-1",
        run_id="run-1",
        child_thread_id="subagent:sess-integration:abc",
        subagent_type="researcher",
        task="find pricing",
        label="researcher",
    )
    await publisher.publish_event(evt)
    assert len(bus._replay) >= 1
    _seq, frame = bus._replay[-1]
    assert "SubagentStarted" in frame
    assert "call-1" in frame
    assert "subagent:sess-integration:abc" in frame
    for line in frame.splitlines():
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            decoded = event_from_json(payload)
            assert isinstance(decoded, SubagentStarted)
            assert decoded.parent_call_id == "call-1"
            break
    else:
        pytest.fail("no data line in SSE frame")
