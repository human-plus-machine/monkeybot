"""Unit tests for RulesEnforcementMW."""

from __future__ import annotations

import pytest

from src.core.harness.errors import RuleViolation
from src.core.harness.identity import LoadedIdentity
from src.core.harness.middleware.rules import RulesEnforcementMW, parse_rules


def test_parse_rules_finds_all_predicates() -> None:
    rules = parse_rules(
        """
        - [R-1] DENY_TOOL: git push
        - [R-2] DENY_PATTERN: \\bdrop\\s+table\\b
        * [R-3] DENY_SANDBOX_WRITE: /etc/**
        Non-rule line
        """
    )
    assert {r.rule_id for r in rules} == {"R-1", "R-2", "R-3"}


@pytest.mark.asyncio
async def test_deny_tool_raises() -> None:
    identity = LoadedIdentity(rules="- [R-1] DENY_TOOL: git push")
    mw = RulesEnforcementMW(identity=identity)
    with pytest.raises(RuleViolation) as exc:
        await mw.check_tool_call("git push", {"branch": "main"})
    assert exc.value.rule_id == "R-1"


@pytest.mark.asyncio
async def test_deny_pattern_raises() -> None:
    identity = LoadedIdentity(rules=r"- [R-2] DENY_PATTERN: (?i)\bdrop\s+table\b")
    mw = RulesEnforcementMW(identity=identity)
    with pytest.raises(RuleViolation):
        await mw.check_tool_call("sql_query", {"query": "DROP TABLE users"})


@pytest.mark.asyncio
async def test_deny_sandbox_write_raises() -> None:
    identity = LoadedIdentity(rules="- [R-3] DENY_SANDBOX_WRITE: /etc/**")
    mw = RulesEnforcementMW(identity=identity)
    with pytest.raises(RuleViolation):
        await mw.check_sandbox_write("/etc/hosts")


@pytest.mark.asyncio
async def test_innocent_tool_call_passes() -> None:
    identity = LoadedIdentity(rules="- [R-1] DENY_TOOL: sudo")
    mw = RulesEnforcementMW(identity=identity)
    await mw.check_tool_call("echo", {"msg": "ok"})
