"""Tests for CommandTierInspector and run_command policy YAML."""

from pathlib import Path

import pytest
import yaml

from monkeybot.core.context import TurnContext
from monkeybot.core.tools.inspector import (
    CommandTierConfigError,
    CommandTierInspector,
    Decision,
    DEFAULT_DENY_PATTERNS,
    InspectorToolCall,
    load_command_tier_policy,
)
from importlib import resources


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
    """Copy packaged policy into tmp_path for isolated inspector construction."""
    content = (resources.files("monkeybot_cli.scaffold_defaults") / "command_allowlist.yaml").read_text(encoding="utf-8")
    dest = tmp_path / "policy.yaml"
    dest.write_text(content, encoding="utf-8")
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
    from monkeybot.core.tools.terminal import ALLOWED_COMMANDS, ALLOWED_PATHS

    path = tmp_path / "t.yaml"
    path.write_text("allowed_commands:\n  - echo\n", encoding="utf-8")
    policy = load_command_tier_policy(path)
    assert policy.allowed_commands == ("echo",)
    assert policy.allowed_path_prefixes == tuple(ALLOWED_PATHS)
    assert policy.deny_patterns == DEFAULT_DENY_PATTERNS


def test_load_policy_explicit_empty_deny_patterns_opt_out(tmp_path: Path) -> None:
    path = tmp_path / "t.yaml"
    path.write_text("deny_patterns: []\n", encoding="utf-8")
    policy = load_command_tier_policy(path)
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


@pytest.mark.parametrize(
    "args",
    [
        {"argv": ["uv", "pip", "install", "google-genai"]},
        {"argv": ["pip", "install", "requests"]},
        {"argv": ["python3", "-m", "pip", "install", "foo"]},
        {"argv": ["bash", "-c", "pip install foo"]},
        {"argv": ["npm", "install", "lodash"]},
        {"argv": ["apt-get", "install", "curl"]},
    ],
)
@pytest.mark.asyncio
async def test_inspector_install_commands_denied(tmp_path: Path, args: dict) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", args)
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "deny"
    assert d.message


@pytest.mark.asyncio
async def test_inspector_uv_run_allowed(tmp_path: Path) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", {"argv": ["uv", "run", "pytest", "tests/"]})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "allow"


@pytest.mark.asyncio
async def test_inspector_skill_script_argv_allowed(tmp_path: Path) -> None:
    p = _write_policy(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall(
        "1",
        "run_command",
        {
            "argv": [
                "python3",
                "./skills/image-generator/generate_image.py",
                "--prompt",
                "test",
            ]
        },
    )
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "allow"
