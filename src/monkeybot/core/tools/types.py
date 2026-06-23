"""Tool execution result types."""

from __future__ import annotations

from dataclasses import dataclass

from monkeybot.core.types.content_blocks import ContentBlock, Text


@dataclass(frozen=True)
class ToolExecutionResult:
    blocks: list[ContentBlock]
    error: str | None = None

    @staticmethod
    def ok_text(text: str) -> ToolExecutionResult:
        return ToolExecutionResult(blocks=[Text(text=text)])

    @staticmethod
    def ok_blocks(blocks: list[ContentBlock]) -> ToolExecutionResult:
        return ToolExecutionResult(blocks=list(blocks))

    @staticmethod
    def err(message: str) -> ToolExecutionResult:
        return ToolExecutionResult(blocks=[Text(text=message)], error=message)


def unwrap_tool_execution_result(result: ToolExecutionResult) -> tuple[str | None, str | None]:
    """Collapse blocks to ``(text, None)`` or ``(None, error)`` for text-only callers."""
    if result.error is not None:
        return None, result.error
    parts = [b.text for b in result.blocks if isinstance(b, Text)]
    return "".join(parts), None
