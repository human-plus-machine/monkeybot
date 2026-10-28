"""Recording executor and the wrapper around the real tool executor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.mcp.mcp_client import MCPClient
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.types import ToolExecutionResult
from regression.drivers.bundles.protocol import LoopDriverError
from regression.schema.scenario import Scenario
from testkit.executors import RecordingExecutor


def _recording_executor(scenario: Scenario) -> RecordingExecutor:
    if scenario.tool_results is None:
        return RecordingExecutor()
    return _MappedRecording(scenario.tool_results)


class _MappedRecording(RecordingExecutor):
    """Recording executor that answers each call id from ``tool_results``."""

    def __init__(self, envelopes: dict[str, dict[str, Any]]) -> None:
        super().__init__()
        self._envelopes = envelopes

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
        del ctx
        self.calls.append(call)
        envelope = self._envelopes.get(call.call_id)
        if envelope is None:
            return self.result
        try:
            text = json.dumps(envelope)
        except TypeError as exc:
            raise LoopDriverError(f"tool_results[{call.call_id}] is not JSON-serializable") from exc
        return ToolExecutionResult.ok_text(text)


class _ObservedExecutor:
    """Records calls while the real executor runs them."""

    def __init__(self, inner: CoreToolExecutor) -> None:
        self._inner = inner
        self.calls: list[ToolCall] = []

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
        self.calls.append(call)
        return await self._inner.execute(call=call, ctx=ctx)

    def retarget_skills(self, skills_path: Path) -> None:
        """Move the skills mount after ``paths.skills_path`` changes.

        The executor captures the root at init. Production builds a new
        executor each user turn; this driver keeps one.
        """
        resolved = Path(skills_path).resolve()
        if self._inner._skills_path == resolved:
            return
        self._inner._skills_path = resolved
        self._inner._workspace._skills_root = resolved

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _shutdown(
    executor: RecordingExecutor | _ObservedExecutor,
    hook_memory: MemorySubsystem | None,
    mcp: MCPClient | None = None,
) -> None:
    closer = getattr(executor, "aclose", None)
    if callable(closer):
        await closer()
    if mcp is not None:
        await mcp.disconnect_all()
    if hook_memory is not None:
        await hook_memory.close()
