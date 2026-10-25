"""Hook payload builder shared by hook tests."""

from __future__ import annotations

from typing import Any

from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookPayload


def _payload(event: HookEvent = HookEvent.PRE_TURN, **kw: Any) -> HookPayload:
    ctx = TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="agent",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
    )
    return HookPayload(event=event, thread_id="t1", request_id="r1", ctx=ctx, **kw)
