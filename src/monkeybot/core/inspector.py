"""Tool inspectors: command tier policy for ``run_command`` (regex YAML)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import yaml
from monkeybot.core.context import TurnContext
from monkeybot.core.interfaces import MonkeybotError


class CommandTierConfigError(MonkeybotError):
    """Invalid or unloadable command tier policy."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


@dataclass(frozen=True)
class Decision:
    """Allow or deny outcome from a tool inspector.

    ``confirm`` is reserved for Story 5; no inspector should return it until then.
    """

    kind: Literal["allow", "deny", "confirm"]
    message: str | None = None


@dataclass(frozen=True)
class InspectorToolCall:
    """Normalized tool call at the inspection boundary (Story 6 maps provider calls here)."""

    call_id: str
    name: str
    args: dict[str, object]


class ToolInspector(Protocol):
    """Pre-flight gate for tool execution."""

    async def check(self, call: InspectorToolCall, ctx: TurnContext) -> Decision:
        """Return allow or deny before executing the tool."""
        ...


@dataclass(frozen=True)
class _Tier:
    name: str
    action: Literal["allow", "deny"]
    patterns: list[str]


def _parse_tier_config(path: Path) -> tuple[list[_Tier], Literal["allow", "deny"]]:
    raw_bytes = path.read_bytes()
    try:
        data = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as e:
        raise CommandTierConfigError(path, f"invalid YAML: {e}") from e

    if not isinstance(data, dict):
        raise CommandTierConfigError(path, "root must be a mapping")

    if "tier_order" not in data:
        raise CommandTierConfigError(path, "missing required key 'tier_order'")
    tier_order = data["tier_order"]
    if not isinstance(tier_order, list) or not tier_order:
        raise CommandTierConfigError(path, "'tier_order' must be a non-empty list")

    if "default" not in data:
        raise CommandTierConfigError(path, "missing required key 'default'")
    default_action = data["default"]
    if default_action not in ("allow", "deny"):
        raise CommandTierConfigError(
            path, f"'default' must be 'allow' or 'deny', got {default_action!r}"
        )

    tiers: list[_Tier] = []
    for i, item in enumerate(tier_order):
        if not isinstance(item, dict):
            raise CommandTierConfigError(path, f"tier_order[{i}] must be a mapping")
        name = item.get("name")
        action = item.get("action")
        patterns = item.get("patterns")
        if not isinstance(name, str) or not name:
            raise CommandTierConfigError(path, f"tier_order[{i}].name must be a non-empty string")
        if action not in ("allow", "deny"):
            raise CommandTierConfigError(
                path, f"tier_order[{i}].action must be 'allow' or 'deny', got {action!r}"
            )
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise CommandTierConfigError(
                path, f"tier_order[{i}].patterns must be a list of strings"
            )
        tiers.append(_Tier(name=name, action=action, patterns=patterns))

    return tiers, default_action


class CommandTierInspector:
    """YAML-driven allow/deny policy for shell commands (regex tiers)."""

    def __init__(self, tier_config_path: Path) -> None:
        self._path = tier_config_path
        self._tiers, self._default = _parse_tier_config(tier_config_path)

    async def check(self, call: InspectorToolCall, ctx: TurnContext) -> Decision:
        """Apply tier rules to ``run_command``; other tools are allowed."""
        del ctx  # policy does not use turn metadata today
        if call.name != "run_command":
            return Decision(kind="allow")

        raw_cmd = call.args.get("command")
        if not isinstance(raw_cmd, str):
            return Decision(
                kind="deny",
                message="run_command requires a string 'command' argument",
            )

        cmd_norm = " ".join(str(raw_cmd).split())
        if not cmd_norm:
            return Decision(kind="deny", message="command must be non-empty")

        for tier in self._tiers:
            for pattern in tier.patterns:
                if re.match(pattern, cmd_norm, flags=re.IGNORECASE):
                    if tier.action == "allow":
                        return Decision(kind="allow")
                    return Decision(
                        kind="deny",
                        message=f"denied by tier {tier.name} policy",
                    )

        if self._default == "allow":
            return Decision(kind="allow")
        return Decision(kind="deny", message="no matching allow rule")


class RulesInspector:
    """Blocks tool calls whose serialized arguments contain any denied substring."""

    def __init__(self, denied_patterns: list[str]) -> None:
        self.denied_patterns = denied_patterns

    async def check(self, call: InspectorToolCall, ctx: TurnContext) -> Decision:
        del ctx
        args_str = str(call.args)
        for pattern in self.denied_patterns:
            if pattern in args_str:
                return Decision(kind="deny", message=f"Blocked by rules: {pattern}")
        return Decision(kind="allow")
