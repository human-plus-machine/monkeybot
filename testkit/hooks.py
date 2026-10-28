"""Hook payload builder and recording hook shared by hook tests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload


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


def payload(event: HookEvent = HookEvent.PRE_TURN, **kw: Any) -> HookPayload:
    """Build a hook payload. ``_payload`` is the same builder."""
    return _payload(event, **kw)


class RecordingHook:
    """Append each fired event. Register on one sequence or every lifecycle point."""

    def __init__(self) -> None:
        self.events: list[HookEvent] = []

    async def __call__(self, payload: HookPayload) -> None:
        self.events.append(payload.event)

    def register(
        self,
        manager: HookManager,
        events: Sequence[HookEvent] | None = None,
    ) -> None:
        chosen = list(HookEvent) if events is None else list(events)
        for event in chosen:
            manager.register(event, self)
