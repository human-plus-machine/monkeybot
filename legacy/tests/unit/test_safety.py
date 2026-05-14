"""Unit tests for load_inspectors factory in core/safety.py."""
from __future__ import annotations

import asyncio

from monkeybot.core.inspector import CommandTierInspector, RulesInspector
from monkeybot.core.provider import ToolCall
from monkeybot.core.safety import load_inspectors

_FULL_TIERS = {
    "pre_approved": ["read_file"],
    "denied": ["rm"],
    "requires_approval": ["write_file"],
}


def check_sync(inspector, tool_name, args=None):  # type: ignore[no-untyped-def]
    """Run an async inspector.check() synchronously for test convenience."""
    call = ToolCall(call_id="test", name=tool_name, args=args or {})
    return asyncio.run(inspector.check(call, ctx=None))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Factory behaviour
# ---------------------------------------------------------------------------


def test_empty_config_returns_empty_list() -> None:
    assert load_inspectors({}) == []


def test_no_safety_key_returns_empty_list() -> None:
    assert load_inspectors({"other_key": 1}) == []


def test_command_tiers_only_returns_one_command_tier_inspector() -> None:
    config = {
        "safety": {
            "command_tiers": {
                "pre_approved": ["read_file"],
                "denied": [],
                "requires_approval": [],
            }
        }
    }
    result = load_inspectors(config)
    assert len(result) == 1
    assert isinstance(result[0], CommandTierInspector)


def test_denied_patterns_only_returns_one_rules_inspector() -> None:
    config = {"safety": {"denied_patterns": ["rm -rf"]}}
    result = load_inspectors(config)
    assert len(result) == 1
    assert isinstance(result[0], RulesInspector)


def test_both_present_returns_two_inspectors_in_order() -> None:
    config = {
        "safety": {
            "command_tiers": _FULL_TIERS,
            "denied_patterns": ["rm -rf"],
        }
    }
    result = load_inspectors(config)
    assert len(result) == 2
    assert isinstance(result[0], CommandTierInspector)
    assert isinstance(result[1], RulesInspector)


def test_safety_none_returns_empty_list() -> None:
    assert load_inspectors({"safety": None}) == []


def test_command_tiers_none_returns_empty_list() -> None:
    assert load_inspectors({"safety": {"command_tiers": None}}) == []


def test_denied_patterns_none_returns_empty_list() -> None:
    assert load_inspectors({"safety": {"denied_patterns": None}}) == []


# ---------------------------------------------------------------------------
# Inspector behaviour — CommandTierInspector
# ---------------------------------------------------------------------------


def test_denied_tool_returns_deny() -> None:
    config = {"safety": {"command_tiers": _FULL_TIERS}}
    inspector = load_inspectors(config)[0]
    decision = check_sync(inspector, "rm")
    assert decision.kind == "deny"


def test_pre_approved_tool_returns_allow() -> None:
    config = {"safety": {"command_tiers": _FULL_TIERS}}
    inspector = load_inspectors(config)[0]
    decision = check_sync(inspector, "read_file")
    assert decision.kind == "allow"


def test_requires_approval_tool_returns_approve() -> None:
    config = {"safety": {"command_tiers": _FULL_TIERS}}
    inspector = load_inspectors(config)[0]
    decision = check_sync(inspector, "write_file")
    assert decision.kind == "approve"


def test_unknown_tool_returns_allow() -> None:
    config = {"safety": {"command_tiers": _FULL_TIERS}}
    inspector = load_inspectors(config)[0]
    decision = check_sync(inspector, "unknown_tool")
    assert decision.kind == "allow"


# ---------------------------------------------------------------------------
# Inspector behaviour — RulesInspector
# ---------------------------------------------------------------------------


def test_args_matching_denied_pattern_returns_deny() -> None:
    config = {"safety": {"denied_patterns": ["rm -rf"]}}
    inspector = load_inspectors(config)[0]
    decision = check_sync(inspector, "bash", args={"cmd": "rm -rf /"})
    assert decision.kind == "deny"


def test_args_not_matching_pattern_returns_allow() -> None:
    config = {"safety": {"denied_patterns": ["rm -rf"]}}
    inspector = load_inspectors(config)[0]
    decision = check_sync(inspector, "bash", args={"cmd": "ls -la"})
    assert decision.kind == "allow"
