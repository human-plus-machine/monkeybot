"""Scripted tool executor shared by loop tests."""

from __future__ import annotations

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


class RecordingExecutor:
    def __init__(
        self,
        result: ToolExecutionResult | tuple[str | None, str | None] = ("ok", None),
    ) -> None:
        self.result = _tool_result(result)
        self.calls: list[ToolCall] = []

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
        del ctx
        self.calls.append(call)
        return self.result
