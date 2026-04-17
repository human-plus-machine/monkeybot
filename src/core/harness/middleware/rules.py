r"""RulesEnforcementMW — deterministic, hard-veto layer that reads RULES.md.

Rule format in RULES.md (one per line, anywhere in the file):
    - [R-1] DENY_TOOL: git push
    - [R-2] DENY_TOOL: rm*
    - [R-3] DENY_PATTERN: \bdrop\s+table\b
    - [R-4] DENY_SANDBOX_WRITE: /etc/**

Everything that is NOT prefixed with ``DENY_`` is treated as soft guidance and
forwarded to the LLM via the system prompt unchanged.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any

from ..errors import RuleViolation
from ..identity import LoadedIdentity


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    predicate: str  # DENY_TOOL | DENY_PATTERN | DENY_SANDBOX_WRITE
    value: str
    raw: str


_RULE_LINE = re.compile(
    r"""^\s*[-*]\s*
        \[(?P<id>R-[A-Za-z0-9_\-]+)\]\s+
        (?P<predicate>DENY_TOOL|DENY_PATTERN|DENY_SANDBOX_WRITE)
        \s*:\s*(?P<value>.+?)\s*$""",
    re.VERBOSE,
)


def parse_rules(rules_md: str) -> list[_Rule]:
    out: list[_Rule] = []
    for line in rules_md.splitlines():
        m = _RULE_LINE.match(line)
        if m:
            out.append(
                _Rule(
                    rule_id=m.group("id"),
                    predicate=m.group("predicate"),
                    value=m.group("value").strip(),
                    raw=line.strip(),
                )
            )
    return out


def check_action(
    rules: list[_Rule],
    *,
    tool_name: str | None = None,
    tool_args_text: str | None = None,
    sandbox_write_path: str | None = None,
) -> _Rule | None:
    """Return the first matching deny rule or None."""
    for rule in rules:
        if rule.predicate == "DENY_TOOL" and tool_name is not None:
            if fnmatch.fnmatch(tool_name, rule.value):
                return rule
        elif rule.predicate == "DENY_PATTERN" and tool_args_text:
            try:
                if re.search(rule.value, tool_args_text):
                    return rule
            except re.error:
                continue
        elif rule.predicate == "DENY_SANDBOX_WRITE" and sandbox_write_path:
            if fnmatch.fnmatch(sandbox_write_path, rule.value):
                return rule
    return None


class RulesEnforcementMW:
    """Hard-veto middleware. Raises RuleViolation that the assembler converts to a
    synthetic ToolMessage so the LLM replans."""

    name = "RulesEnforcementMW"

    def __init__(self, identity: LoadedIdentity, event_bus: Any | None = None) -> None:
        self.rules = parse_rules(identity.rules)
        self.event_bus = event_bus

    async def check_tool_call(
        self, tool_name: str, tool_args: dict[str, Any] | str
    ) -> None:
        args_text = tool_args if isinstance(tool_args, str) else _flatten_args(tool_args)
        match = check_action(self.rules, tool_name=tool_name, tool_args_text=args_text)
        if match:
            await self._emit_veto(match, action=f"tool:{tool_name}({args_text[:200]})")
            raise RuleViolation(rule_id=match.rule_id, rule_text=match.raw, action=f"tool:{tool_name}")

    async def check_sandbox_write(self, path: str) -> None:
        match = check_action(self.rules, sandbox_write_path=path)
        if match:
            await self._emit_veto(match, action=f"sandbox_write:{path}")
            raise RuleViolation(rule_id=match.rule_id, rule_text=match.raw, action=f"sandbox_write:{path}")

    async def _emit_veto(self, rule: _Rule, *, action: str) -> None:
        if self.event_bus is None:
            return
        try:
            from ..events import EventKind, HarnessEvent, Principal, VersionTriple  # lazy
            from datetime import UTC, datetime

            await self.event_bus.publish(
                HarnessEvent(
                    run_id="n/a",
                    session_id="n/a",
                    principal=Principal(),
                    versions=VersionTriple(harness="1", deep_agents="n/a", model="n/a"),
                    ts=datetime.now(UTC),
                    kind=EventKind.RULE_VETO,
                    payload={"rule_id": rule.rule_id, "rule_text": rule.raw, "action": action},
                )
            )
        except Exception:  # pragma: no cover — observability must never crash the request
            pass


def _flatten_args(args: dict[str, Any]) -> str:
    try:
        import json

        return json.dumps(args, default=str, sort_keys=True)
    except Exception:
        return str(args)
