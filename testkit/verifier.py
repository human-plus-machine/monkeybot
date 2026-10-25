"""Verifier doubles shared by goal-ledger and progress-tracker tests."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookPayload
from monkeybot.core.persistence.goal_ledger import Classification, GoalEntry, Intent
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.core.verifier.mailbox import VerdictMailbox


class ScriptedClassifier:
    """Test double: returns queued classifications, optionally delaying."""

    def __init__(
        self,
        results: Sequence[Classification],
        *,
        delay_s: float = 0.0,
    ) -> None:
        self._results = list(results)
        self._delay_s = delay_s
        self.calls: list[str] = []

    async def classify(
        self,
        verbatim: str,
        open_entries: Sequence[GoalEntry],
        *,
        thread_id: str = "",
    ) -> Classification:
        del open_entries, thread_id
        self.calls.append(verbatim)
        if self._delay_s > 0:
            await asyncio.sleep(self._delay_s)
        if not self._results:
            return Classification(intent=Intent.NOISE, relates_to=None, constraints=())
        return self._results.pop(0)


def _mailbox(thread_id: str = "t1", request_id: str = "r1") -> VerdictMailbox:
    mailbox = VerdictMailbox()
    mailbox.open_request(thread_id, request_id)
    return mailbox


def _tracker_ctx() -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[ToolDef("write_file", "w", {})],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )


def verifier_payload(
    *,
    event: HookEvent,
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
    tool_error: str | None = None,
    inner_turn: int = 3,
    assistant_text: str | None = None,
    tool_requests: list[Any] | None = None,
    usage: dict[str, Any] | None = None,
) -> HookPayload:
    return HookPayload(
        event=event,
        thread_id="t1",
        request_id="r1",
        ctx=_tracker_ctx(),
        tool_name=tool_name,
        tool_args=tool_args,
        tool_error=tool_error,
        inner_turn=inner_turn,
        assistant_text=assistant_text,
        tool_requests=tool_requests,
        usage=usage,
    )
