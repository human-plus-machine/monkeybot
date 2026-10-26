"""Scripted tool executor shared by loop tests."""

from __future__ import annotations

import asyncio
import os

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.tools.types import ToolExecutionResult


def _tool_result(
    result: ToolExecutionResult | tuple[str | None, str | None],
) -> ToolExecutionResult:
    if isinstance(result, ToolExecutionResult):
        return result
    text, err = result
    if err is not None:
        return ToolExecutionResult.err(err)
    return ToolExecutionResult.ok_text(text or "")


def _parallel_overlap_cap() -> int | None:
    """Cap from ``MONKEYBOT_PARALLEL_TOOL_CONCURRENCY``, or None when unset.

    Unset keeps ``execute`` synchronous. A set cap yields once so concurrent
    calls overlap, and a call that sees more than the cap in flight fails.
    """
    raw = os.environ.get("MONKEYBOT_PARALLEL_TOOL_CONCURRENCY")
    if raw is None or not raw.strip():
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        return None


class RecordingExecutor:
    def __init__(
        self,
        result: ToolExecutionResult | tuple[str | None, str | None] = ("ok", None),
    ) -> None:
        self.result = _tool_result(result)
        self.calls: list[ToolCall] = []
        self.max_in_flight = 0
        self._in_flight = 0

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
        del ctx
        self.calls.append(call)
        cap = _parallel_overlap_cap()
        if cap is None:
            return self.result
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            await asyncio.sleep(0)
            if self._in_flight > cap:
                return ToolExecutionResult.err(f"parallel overlap {self._in_flight} exceeded {cap}")
            return self.result
        finally:
            self._in_flight -= 1

    def running_background_jobs(self) -> list[object]:
        """Empty unless the scenario asks the job check to fail closed.

        The loop calls this when the executor has it. Unset, the check sees no
        jobs. ``MONKEYBOT_TEST_BACKGROUND_JOBS_RAISE=1`` raises so a scenario
        can observe the fail-closed error without a product change.
        """
        raw = os.environ.get("MONKEYBOT_TEST_BACKGROUND_JOBS_RAISE", "").strip().lower()
        if raw in {"1", "true", "yes"}:
            raise RuntimeError("background job check failed")
        return []
