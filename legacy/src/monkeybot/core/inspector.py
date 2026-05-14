"""
ToolInspector Protocol — the safety surface.
Loop iterates a list of inspectors before every tool call.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from .context import TurnContext
from .provider import ToolCall


@dataclass
class Decision:
    """Outcome of an inspector check.

    Attributes:
        kind: One of "allow", "deny", or "approve".
        message: Optional explanation for deny/approve decisions.
    """

    kind: Literal["allow", "deny", "approve"]
    message: str | None = None


@runtime_checkable
class ToolInspector(Protocol):
    """Protocol for tool call safety inspectors.

    Use @runtime_checkable so isinstance() checks work without subclassing.
    """

    async def check(self, call: ToolCall, ctx: TurnContext) -> Decision:
        """Inspect a tool call and return a decision.

        Args:
            call: The tool call to inspect.
            ctx: The current turn context.

        Returns:
            A Decision indicating whether to allow, deny, or request approval.
        """
        ...


class CommandTierInspector:
    """Classifies tool calls into tiers based on YAML config.

    pre_approved: always allow.
    requires_approval: emit ApprovalRequest, pause loop.
    denied: always block.

    Attributes:
        pre_approved: Set of tool names that are always allowed.
        requires_approval: Set of tool names that need human approval.
        denied: Set of tool names that are always blocked.
    """

    def __init__(self, config: dict) -> None:  # type: ignore[type-arg]
        """Initialize with a tier configuration dict.

        Args:
            config: Dict with optional keys "pre_approved", "requires_approval", "denied",
                each mapping to a list of tool name strings.
        """
        self.pre_approved: set[str] = set(config.get("pre_approved", []))
        self.requires_approval: set[str] = set(config.get("requires_approval", []))
        self.denied: set[str] = set(config.get("denied", []))

    async def check(self, call: ToolCall, ctx: TurnContext) -> Decision:
        """Check a tool call against configured tiers.

        Args:
            call: The tool call to inspect.
            ctx: The current turn context.

        Returns:
            Decision based on which tier the tool name falls into.
            Unlisted tools default to allow.
        """
        if call.name in self.denied:
            return Decision(kind="deny", message=f"{call.name} is not permitted")
        if call.name in self.requires_approval:
            return Decision(kind="approve", message=f"{call.name} requires approval")
        return Decision(kind="allow")


class RulesInspector:
    """Blocks tool calls whose arguments match any denied pattern.

    Attributes:
        denied_patterns: Substrings that trigger a deny decision.
    """

    def __init__(self, denied_patterns: list[str]) -> None:
        """Initialize with a list of denied argument patterns.

        Args:
            denied_patterns: Substrings to search for in serialized args.
        """
        self.denied_patterns = denied_patterns

    async def check(self, call: ToolCall, ctx: TurnContext) -> Decision:
        """Check tool call arguments against denied patterns.

        Args:
            call: The tool call to inspect.
            ctx: The current turn context.

        Returns:
            Decision deny if any pattern matches args, else allow.
        """
        args_str = str(call.args)
        for pattern in self.denied_patterns:
            if pattern in args_str:
                return Decision(kind="deny", message=f"Blocked by rules: {pattern}")
        return Decision(kind="allow")
