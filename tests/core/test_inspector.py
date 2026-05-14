"""Tests for CommandTierInspector and YAML fail-fast loading."""

from pathlib import Path

import pytest
import yaml
from monkeybot.core.context import TurnContext
from monkeybot.core.inspector import (
    CommandTierConfigError,
    CommandTierInspector,
    InspectorToolCall,
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


def _write_tiers(tmp_path: Path) -> Path:
    """Copy repo policy into tmp_path for isolated inspector construction."""
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "config" / "command_tiers.yaml"
    dest = tmp_path / "tiers.yaml"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


@pytest.mark.asyncio
async def test_inspector_safe_python_skills_allowed(tmp_path: Path) -> None:
    p = _write_tiers(tmp_path)
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
async def test_inspector_destructive_rm_denied(tmp_path: Path) -> None:
    p = _write_tiers(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", {"command": "rm -rf /"})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "deny"
    assert d.message


@pytest.mark.asyncio
async def test_inspector_restricted_curl_denied(tmp_path: Path) -> None:
    p = _write_tiers(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", {"command": "curl http://x"})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "deny"


@pytest.mark.asyncio
async def test_inspector_unknown_command_denied(tmp_path: Path) -> None:
    p = _write_tiers(tmp_path)
    inspector = CommandTierInspector(p)
    call = InspectorToolCall("1", "run_command", {"command": "touch a"})
    d = await inspector.check(call, _minimal_ctx())
    assert d.kind == "deny"
    assert d.message == "no matching allow rule"


@pytest.mark.asyncio
async def test_inspector_non_run_command_bypassed(tmp_path: Path) -> None:
    p = _write_tiers(tmp_path)
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


def test_inspector_missing_tier_order_raises(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text(yaml.safe_dump({}), encoding="utf-8")
    with pytest.raises(CommandTierConfigError, match="tier_order"):
        CommandTierInspector(path)


def test_inspector_missing_default_raises(tmp_path: Path) -> None:
    path = tmp_path / "nodefault.yaml"
    path.write_text(
        yaml.safe_dump({"tier_order": [{"name": "safe", "action": "allow", "patterns": []}]}),
        encoding="utf-8",
    )
    with pytest.raises(CommandTierConfigError, match="default"):
        CommandTierInspector(path)
