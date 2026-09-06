"""Synchronous read of cached verdict state at the tool-inspection boundary."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from monkeybot.core.context import TurnContext
from monkeybot.core.logging_utils import kv
from monkeybot.core.tools.inspector import Decision, InspectorToolCall
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.core.verifier.mailbox import VerdictMailbox
from monkeybot.core.verifier.severity import cap_severity

logger = logging.getLogger(__name__)


def _is_read_only(name: str, tools: Sequence[ToolDef]) -> bool:
    for tool in tools:
        if tool.name == name:
            return tool.read_only
    return False


class VerifierInspector:
    """Deny mutating tools when the latest capped verdict is ``block``. Fail-open.

    A ``block`` is request-scoped: it expires when ``request_id`` changes so a
    later user message is not stuck behind a stale deny.
    """

    def __init__(self, mailbox: VerdictMailbox) -> None:
        self._mailbox = mailbox

    async def check(self, call: InspectorToolCall, ctx: TurnContext) -> Decision:
        try:
            last = self._mailbox.last(ctx.thread_id)
            if last is None or last.severity == "none":
                return Decision(kind="allow")
            if last.request_id and last.request_id != ctx.request_id:
                return Decision(kind="allow")
            max_sev = "nudge"
            if ctx.config is not None:
                max_sev = ctx.config.verifier.escalation.max_severity
            if cap_severity(last.severity, max_sev) != "block":
                return Decision(kind="allow")
            if _is_read_only(call.name, ctx.tools):
                return Decision(kind="allow")
            message = last.correction or last.rationale or "blocked by verifier"
            logger.info(
                "verifier inspector deny %s",
                kv(
                    thread_id=ctx.thread_id,
                    request_id=ctx.request_id,
                    tool=call.name,
                    rationale=last.rationale,
                ),
            )
            return Decision(kind="deny", message=message)
        except Exception:
            logger.warning(
                "verifier inspector failed %s",
                kv(thread_id=ctx.thread_id, request_id=ctx.request_id),
                exc_info=True,
            )
            return Decision(kind="allow")
