"""Tests for CommandTierInspector and run_command policy YAML."""

from pathlib import Path

import pytest
import yaml

from monkeybot.core.context import TurnContext
from monkeybot.core.inspector import (
    CommandTierConfigError,
    CommandTierInspector,
    Decision,
    InspectorToolCall,
    load_command_tier_policy,
)


def _minimal_ctx() -> TurnContext:
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
    )


def _write_policy(tmp_path: Path) -> Path:
    """Copy repo policy into tmp_path for isolated inspector construction."""
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "monkeybot_config" / "command_allowlist.yaml"
    dest = tmp_path / "policy.yaml"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


@pytest.mark.asyncio
async def test_inspector_python_skills_allowed_when_no_deny_match(tmp_path: Path) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall(
        "1",
        "run_command",
        {"command": "python skills/foo/run.py"},
    )
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "allow"
    assert d.message is None


@pytest.mark.asyncio
async def test_inspector_destructive_rm_denied_by_deny_pattern(tmp_path: Path) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", {"command": "rm -rf /"})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "deny"
    assert d.message


@pytest.mark.asyncio
async def test_inspector_restricted_curl_denied(tmp_path: Path) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", {"command": "curl http://x"})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "deny"


@pytest.mark.asyncio
async def test_inspector_unknown_command_allowed_at_preflight(tmp_path: Path) -> None:
    """Deny-regex policy does not emulate an allow-regex tier; unknown invocations pass here."""
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", {"command": "touch a"})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "allow"


@pytest.mark.asyncio
async def test_inspector_argv_used_for_deny_check(tmp_path: Path) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", {"argv": ["curl", "http://x"]})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "deny"


@pytest.mark.asyncio
async def test_inspector_non_run_command_bypassed(tmp_path: Path) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "read_file", {"path": "x"})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "allow"


@pytest.mark.asyncio
async def test_inspector_invalid_yaml_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("[broken", encoding="utf-8")
    with pytest.raises(CommandTierConfigError, match="invalid YAML"):
        CommandTierInspector(bad)


def test_inspector_obsolete_tier_order_raises(tmp_path: Path) -> None:
    path = tmp_path / "legacy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "tier_order": [{"name": "x", "action": "allow", "patterns": [".*"]}],
                "default": "allow",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CommandTierConfigError, match="obsolete key"):
        CommandTierInspector(path)


def test_decision_confirm_variant_constructible() -> None:
    d = Decision(kind="confirm", message=None)
    assert d.kind == "confirm"
    assert d.message is None


def test_load_policy_omitted_allowlists_use_terminal_defaults(tmp_path: Path) -> None:
    from monkeybot.core.terminal import ALLOWED_COMMANDS, ALLOWED_PATHS

    path = tmp_path / "t.yaml"
    path.write_text("deny_patterns: []\n", encoding="utf-8")
    policy = load_command_tier_policy(path)
    assert policy.allowed_commands == tuple(ALLOWED_COMMANDS)
    assert policy.allowed_path_prefixes == tuple(ALLOWED_PATHS)
    assert policy.deny_patterns == ()


def test_load_policy_empty_allowed_commands_raises(tmp_path: Path) -> None:
    path = tmp_path / "t.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "allowed_commands": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CommandTierConfigError, match="allowed_commands"):
        load_command_tier_policy(path)


def test_load_policy_invalid_deny_regex_raises(tmp_path: Path) -> None:
    path = tmp_path / "t.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "deny_patterns": ["("],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CommandTierConfigError, match="invalid regex"):
        CommandTierInspector(path)
