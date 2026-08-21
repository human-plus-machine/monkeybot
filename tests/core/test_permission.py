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
    remember_always_approval,
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


def test_resource_for_url() -> None:
    """computer_open_url has no `path` arg — must still get a readable resource string."""
    call = InspectorToolCall("1", "computer_open_url", {"url": "https://example.com"})
    assert resource_for_call(call) == "https://example.com"


def test_resource_for_app() -> None:
    call = InspectorToolCall("1", "computer_open_app", {"app": "Notes"})
    assert resource_for_call(call) == "Notes"


def test_resource_for_path_still_wins_over_url_and_app() -> None:
    """path stays highest priority — url/app are only a fallback below it."""
    call = InspectorToolCall("1", "computer_open", {"path": "/x", "url": "https://example.com"})
    assert resource_for_call(call) == "/x"


def test_existing_tools_unaffected_by_url_app_lookups() -> None:
    """Adding url/app lookups must not change any resource string that previously
    fell through to the JSON fallback for tools with neither arg shape."""
    call = InspectorToolCall("1", "list_skills", {})
    assert resource_for_call(call) == "{}"


def test_url_app_lookups_scoped_to_computer_tools() -> None:
    """A non-computer tool (e.g. an MCP tool) that happens to take a `url` or
    `app` arg must not have its resource string silently change — only
    computer_* tools get the url/app fallback."""
    call = InspectorToolCall("1", "some_mcp_tool", {"url": "https://example.com"})
    assert resource_for_call(call) == '{"url": "https://example.com"}'


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


def test_remember_always_approval_calls_persist_hook_that_approves() -> None:
    class _Bus:
        session_approvals = SessionApprovals()

    bus = _Bus()
    calls: list[tuple[str, str]] = []

    def _approve(t: str, r: str) -> bool:
        calls.append((t, r))
        return True

    remember_always_approval(bus, "computer_open", "/x", persist=_approve)
    assert calls == [("computer_open", "/x")]
    assert bus.session_approvals.is_allowed("computer_open", "/x")


def test_remember_always_approval_persist_hook_can_veto_session_remember() -> None:
    """A persist hook returning False must skip the in-memory remember too — not
    only the durable write. This is what lets the computer_* tools refuse to
    remember `computer_move`/`computer_trash` at all (their resource is the
    source path only, so an in-session-only remember would be just as wrong as a
    durable one — see computer/permissions.py::build_persist_hook)."""

    class _Bus:
        session_approvals = SessionApprovals()

    bus = _Bus()
    remember_always_approval(bus, "computer_move", "/x", persist=lambda t, r: False)
    assert not bus.session_approvals.is_allowed("computer_move", "/x")


def test_remember_always_approval_without_persist_is_unchanged() -> None:
    class _Bus:
        session_approvals = SessionApprovals()

    bus = _Bus()
    remember_always_approval(bus, "write_file", "/x")  # no persist kwarg — default None
    assert bus.session_approvals.is_allowed("write_file", "/x")


def test_remember_always_approval_persist_failure_fails_closed() -> None:
    """A broken persist hook must not break the turn (the action was already
    approved and executed) — but it also must not silently grant a standing
    approval it couldn't actually persist. Skipping the remember is the safe
    direction: the user is just asked again next time."""

    class _Bus:
        session_approvals = SessionApprovals()

    bus = _Bus()

    def _boom(tool: str, resource: str) -> bool:
        raise RuntimeError("disk full")

    remember_always_approval(bus, "computer_open", "/x", persist=_boom)
    assert not bus.session_approvals.is_allowed("computer_open", "/x")


def test_session_approvals_clear() -> None:
    approvals = SessionApprovals()
    approvals.remember("computer_open", "/x")
    assert approvals.is_allowed("computer_open", "/x")
    approvals.clear()
    assert not approvals.is_allowed("computer_open", "/x")


def test_packaged_permissions_loads() -> None:
    from importlib import resources

    path = Path(str(resources.files("monkeybot_cli.scaffold_defaults") / "permissions.yaml"))
    ruleset = load_permissions(path)
    assert ruleset.default == "allow"
    assert ruleset.rules == ()
