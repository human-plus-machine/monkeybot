"""Ports for the agent turn loop (fakeable boundaries)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.tools.types import ToolExecutionResult


@runtime_checkable
class ToolExecutorPort(Protocol):
    """Fakeable tool execution boundary (Story 6 does not invoke real shell)."""

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
        """Return content blocks for history; ``error`` set on failure."""
