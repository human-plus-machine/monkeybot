"""Tests for permission ruleset DSL (P3.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from monkeybot.core.context import TurnContext
from monkeybot.core.tools.inspector import Decision, InspectorToolCall
from monkeybot.core.tools.permission import (
    PermissionConfigError,
    PermissionInspector,
    PermissionRule,
    PermissionRuleset,
    SessionApprovals,
    evaluate,
    load_permissions,
    resource_for_call,
)


def _ctx(*, bus: object | None = None) -> TurnContext:
    return TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
        sse_bus=bus,  # type: ignore[arg-type]
    )


def test_evaluate_last_match_wins() -> None:
    rules = (
        PermissionRule(tool="run_command", pattern="*", effect="ask"),
        PermissionRule(tool="run_command", pattern="git *", effect="allow"),
        PermissionRule(tool="run_command", pattern="rm *", effect="deny"),
    )
    assert evaluate("run_command", "git status", rules).effect == "allow"  # type: ignore[union-attr]
    assert evaluate("run_command", "rm -rf /", rules).effect == "deny"  # type: ignore[union-attr]
    assert evaluate("run_command", "ls", rules).effect == "ask"  # type: ignore[union-attr]
    assert evaluate("read_file", "x", rules) is None


def test_resource_for_run_command() -> None:
    call = InspectorToolCall("1", "run_command", {"command": "git status"})
    assert resource_for_call(call) == "git status"


def test_resource_for_path() -> None:
    call = InspectorToolCall("1", "write_file", {"path": "./foo.txt", "content": "x"})
    assert resource_for_call(call) == "./foo.txt"


@pytest.mark.asyncio
async def test_inspector_default_allow(tmp_path: Path) -> None:
    p = tmp_path / "permissions.yaml"
    p.write_text("default: allow\nrules: []\n", encoding="utf-8")
    insp = PermissionInspector(load_permissions(p))
    d = await insp.check(
        InspectorToolCall("1", "read_file", {"path": "a"}), _ctx()
    )
    assert d == Decision(kind="allow")


@pytest.mark.asyncio
async def test_inspector_ask_and_deny(tmp_path: Path) -> None:
    p = tmp_path / "permissions.yaml"
    p.write_text(
        yaml.dump(
            {
                "default": "allow",
                "rules": [
                    {
                        "tool": "write_file",
                        "pattern": "*",
                        "effect": "ask",
                        "message": "Approve write?",
                    },
                    {
                        "tool": "run_command",
                        "pattern": "rm *",
                        "effect": "deny",
                        "message": "no rm",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    insp = PermissionInspector(load_permissions(p))
    ask = await insp.check(
        InspectorToolCall("1", "write_file", {"path": "a"}), _ctx()
    )
    assert ask.kind == "confirm"
    assert ask.message == "Approve write?"
    deny = await insp.check(
        InspectorToolCall("2", "run_command", {"command": "rm -rf x"}), _ctx()
    )
    assert deny.kind == "deny"
    assert deny.message == "no rm"


@pytest.mark.asyncio
async def test_session_approvals_short_circuit() -> None:
    class _Bus:
        session_approvals = SessionApprovals()

    bus = _Bus()
    call = InspectorToolCall("1", "write_file", {"path": "./a.txt"})
    bus.session_approvals.remember(call.name, resource_for_call(call))
    ruleset = PermissionRuleset(
        rules=(PermissionRule(tool="write_file", pattern="*", effect="ask"),),
        default="allow",
    )
    insp = PermissionInspector(ruleset)
    d = await insp.check(call, _ctx(bus=bus))
    assert d.kind == "allow"


@pytest.mark.asyncio
async def test_inspector_ask_denies_when_interactive_disallowed(tmp_path: Path) -> None:
    """Subagents (no session to prompt) must never surface a confirm; ``ask`` -> deny."""
    p = tmp_path / "permissions.yaml"
    p.write_text(
        yaml.dump(
            {
                "default": "allow",
                "rules": [
                    {
                        "tool": "write_file",
                        "pattern": "*",
                        "effect": "ask",
                        "message": "Approve write?",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    insp = PermissionInspector(load_permissions(p), allow_ask=False)
    d = await insp.check(
        InspectorToolCall("1", "write_file", {"path": "a"}), _ctx()
    )
    assert d.kind == "deny"
    assert d.message == "Approve write?"


def test_load_permissions_invalid_effect(tmp_path: Path) -> None:
    p = tmp_path / "permissions.yaml"
    p.write_text("default: maybe\nrules: []\n", encoding="utf-8")
    with pytest.raises(PermissionConfigError):
        load_permissions(p)


def test_packaged_permissions_loads() -> None:
    from importlib import resources

    path = Path(str(resources.files("monkeybot.monkeybot_config") / "permissions.yaml"))
    ruleset = load_permissions(path)
    assert ruleset.default == "allow"
    assert ruleset.rules == ()
