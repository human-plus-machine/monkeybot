"""Permission ruleset DSL: last-match-wins allow / ask / deny + session approvals.

Soft preflight layer on top of hard execution constraints (command allowlists +
sandbox). Patterns use ``fnmatch`` wildcards (``*``, ``?``) against tool name and
a normalized resource string.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

import yaml

from monkeybot.core.context import TurnContext
from monkeybot.core.logging_utils import kv
from monkeybot.core.tools.inspector import (
    Decision,
    InspectorToolCall,
    norm_run_command_line,
)
from monkeybot.core.types.interfaces import MonkeybotError

logger = logging.getLogger(__name__)

Effect = Literal["allow", "ask", "deny"]


class PermissionConfigError(MonkeybotError):
    """Invalid or unloadable permissions YAML."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


@dataclass(frozen=True)
class PermissionRule:
    """One ruleset entry. Evaluation is last-match-wins across the ordered list."""

    tool: str
    pattern: str
    effect: Effect
    message: str | None = None


@dataclass(frozen=True)
class PermissionRuleset:
    """Loaded permissions config."""

    rules: tuple[PermissionRule, ...]
    default: Effect = "allow"


def resource_for_call(call: InspectorToolCall) -> str:
    """Normalize a tool call into a single resource string for pattern matching."""
    if call.name == "run_command":
        try:
            line = norm_run_command_line(call.args)
        except ValueError as e:
            logger.debug(
                "permission: argv coerce failed for resource string %s",
                kv(tool=call.name, call_id=call.call_id, error=str(e)),
            )
            line = None
        if line:
            return line
    path = call.args.get("path")
    if isinstance(path, str) and path.strip():
        return path.strip()
    argv = call.args.get("argv")
    if isinstance(argv, list) and argv:
        return shlex.join(str(x) for x in argv)
    command = call.args.get("command")
    if isinstance(command, str) and command.strip():
        return " ".join(command.strip().split())
    # Stable fallback for tool calls with none of the recognized shapes above.
    # Sort keys so the same logical args produce the same resource string
    # regardless of dict insertion order (e.g. across provider round-trips).
    return json.dumps(call.args, sort_keys=True, default=str)


def _match(rule: PermissionRule, tool: str, resource: str) -> bool:
    return fnmatch.fnmatchcase(tool, rule.tool) and fnmatch.fnmatchcase(
        resource, rule.pattern
    )


def evaluate(tool: str, resource: str, rules: tuple[PermissionRule, ...]) -> PermissionRule | None:
    """Return the last matching rule, or ``None`` if nothing matched."""
    matched: PermissionRule | None = None
    for rule in rules:
        if _match(rule, tool, resource):
            matched = rule
    return matched


def load_permissions(path: Path) -> PermissionRuleset:
    """Load ``permissions.yaml`` (``default`` + ordered ``rules``)."""
    raw_bytes = path.read_bytes()
    try:
        data = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as e:
        raise PermissionConfigError(path, f"invalid YAML: {e}") from e

    if data is None:
        return PermissionRuleset(rules=(), default="allow")
    if not isinstance(data, dict):
        raise PermissionConfigError(path, "root must be a mapping")

    default_raw = data.get("default", "allow")
    if default_raw not in ("allow", "ask", "deny"):
        raise PermissionConfigError(
            path, f"'default' must be allow|ask|deny, got {default_raw!r}"
        )
    default = cast(Effect, default_raw)

    rules_raw = data.get("rules", [])
    if rules_raw is None:
        rules_raw = []
    if not isinstance(rules_raw, list):
        raise PermissionConfigError(path, "'rules' must be a list")

    rules: list[PermissionRule] = []
    for i, item in enumerate(rules_raw):
        if not isinstance(item, dict):
            raise PermissionConfigError(path, f"rules[{i}] must be a mapping")
        tool = item.get("tool")
        pattern = item.get("pattern", "*")
        effect = item.get("effect")
        message = item.get("message")
        if not isinstance(tool, str) or not tool.strip():
            raise PermissionConfigError(path, f"rules[{i}].tool must be a non-empty string")
        if not isinstance(pattern, str) or not pattern.strip():
            raise PermissionConfigError(
                path, f"rules[{i}].pattern must be a non-empty string"
            )
        if effect not in ("allow", "ask", "deny"):
            raise PermissionConfigError(
                path, f"rules[{i}].effect must be allow|ask|deny, got {effect!r}"
            )
        if message is not None and not isinstance(message, str):
            raise PermissionConfigError(path, f"rules[{i}].message must be a string")
        rules.append(
            PermissionRule(
                tool=tool.strip(),
                pattern=pattern.strip(),
                effect=cast(Effect, effect),
                message=message,
            )
        )

    return PermissionRuleset(rules=tuple(rules), default=default)


@dataclass
class SessionApprovals:
    """In-memory session-scoped 'always allow' rememberies (OpenCode ``reply: always``).

    Process-local only (same durability class as ``SessionBus.admission``): not
    shared across gateway replicas and lost on restart.
    """

    _keys: set[tuple[str, str]] = field(default_factory=set)

    def is_allowed(self, tool: str, resource: str) -> bool:
        return (tool, resource) in self._keys

    def remember(self, tool: str, resource: str) -> None:
        self._keys.add((tool, resource))


def remember_always_approval(bus: object | None, tool: str, resource: str) -> None:
    """Record a session "always allow" approval on ``bus.session_approvals`` if present.

    Shared by the text (``loop.py``) and realtime (``realtime_loop.py``) HITL confirm
    paths so the ``payload.get("always")`` handling isn't duplicated across both.
    No-op when ``bus`` is ``None`` or has no ``session_approvals`` attribute.
    """
    approvals = getattr(bus, "session_approvals", None)
    if approvals is not None:
        approvals.remember(tool, resource)


def _decision_from_effect(effect: Effect, message: str | None) -> Decision:
    if effect == "allow":
        return Decision(kind="allow")
    if effect == "deny":
        return Decision(kind="deny", message=message or "denied by permission ruleset")
    return Decision(kind="confirm", message=message or "Approval required for this tool call")


class PermissionInspector:
    """``ToolInspector`` that evaluates a last-match-wins permission ruleset.

    Session approvals (when present on ``ctx.sse_bus.session_approvals``) short-circuit
    to allow before ruleset evaluation.

    ``allow_ask`` controls whether an ``ask`` effect may prompt interactively
    (``confirm``, the default) or must resolve deterministically. Set
    ``allow_ask=False`` for non-interactive callers (e.g. subagents) that have
    no session to prompt: ``ask`` is then treated as ``deny`` rather than
    surfacing a confirmation that can never be answered.
    """

    def __init__(self, ruleset: PermissionRuleset, *, allow_ask: bool = True) -> None:
        self._ruleset = ruleset
        self._allow_ask = allow_ask

    @property
    def ruleset(self) -> PermissionRuleset:
        return self._ruleset

    async def check(self, call: InspectorToolCall, ctx: TurnContext) -> Decision:
        resource = resource_for_call(call)
        bus = ctx.sse_bus
        if bus is not None:
            approvals = getattr(bus, "session_approvals", None)
            if isinstance(approvals, SessionApprovals) and approvals.is_allowed(
                call.name, resource
            ):
                logger.debug(
                    "permission session allow %s",
                    kv(tool=call.name, call_id=call.call_id, resource=resource),
                )
                return Decision(kind="allow")

        matched = evaluate(call.name, resource, self._ruleset.rules)
        effect = self._ruleset.default if matched is None else matched.effect
        message = None if matched is None else matched.message
        if effect == "ask" and not self._allow_ask:
            logger.debug(
                "permission ask->deny (no interactive session) %s",
                kv(tool=call.name, call_id=call.call_id, resource=resource),
            )
            return Decision(
                kind="deny",
                message=message or "ask effect requires an interactive session; denied",
            )
        return _decision_from_effect(effect, message)


def try_load_permission_inspector(
    path: Path, *, allow_ask: bool = True
) -> PermissionInspector | None:
    """Load ``permissions.yaml`` into an inspector, or ``None`` if missing/invalid.

    Missing file → ``None`` (logged at info). Invalid YAML → ``None`` (logged at
    exception). Soft ruleset is fail-open; hard allowlists/sandbox still apply.
    ``allow_ask`` is forwarded to :class:`PermissionInspector` (set ``False`` for
    non-interactive callers such as subagents; ``ask`` then resolves to ``deny``).
    """
    try:
        ruleset = load_permissions(path)
    except FileNotFoundError:
        logger.info("permissions.yaml missing (%s); soft ruleset disabled", path)
        return None
    except PermissionConfigError:
        logger.exception("permission ruleset load failed (%s)", path)
        return None
    except Exception:
        logger.exception("permission ruleset load failed (%s)", path)
        return None
    logger.info(
        "permission ruleset loaded (%s rules, default=%s, allow_ask=%s)",
        len(ruleset.rules),
        ruleset.default,
        allow_ask,
    )
    return PermissionInspector(ruleset, allow_ask=allow_ask)


__all__ = [
    "Effect",
    "PermissionConfigError",
    "PermissionInspector",
    "PermissionRule",
    "PermissionRuleset",
    "SessionApprovals",
    "evaluate",
    "load_permissions",
    "remember_always_approval",
    "resource_for_call",
    "try_load_permission_inspector",
]
