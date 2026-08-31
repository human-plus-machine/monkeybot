"""Unit tests for GatewayRuntime slice builders (reload-safe inspector wiring)."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.layout import AgentLayout
from monkeybot.core.tools.inspector import CommandTierInspector, RulesInspector
from monkeybot.core.tools.loop_inspector import LoopStartInspector
from monkeybot.gateway.sse.app import GatewayRuntime


def _layout(tmp_path: Path, *, command_allowlist: Path) -> AgentLayout:
    return AgentLayout(
        agent_root=tmp_path,
        config_path=None,
        config_dir=tmp_path / "monkeybot_config",
        workspace_root=tmp_path,
        skills_path=tmp_path / "skills",
        artifacts_path=None,
        data_root=tmp_path / "data",
        agent_md_path=tmp_path / "AGENT.md",
        mcp_config_path=tmp_path / "mcp.json",
        command_allowlist_path=command_allowlist,
        permission_config_path=tmp_path / "permissions.yaml",
        approvals_path=tmp_path / "approvals.json",
        db_url="sqlite:///:memory:",
        memory_storage_uri="local://memory",
        agent_id="test",
    )


def _build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    command_allowlist: Path,
    denied: str | None = None,
) -> GatewayRuntime:
    monkeypatch.setattr(
        "monkeybot.gateway.sse.app._computer_tools_wanted", lambda _cfg: False
    )
    if denied is None:
        monkeypatch.delenv("MONKEYBOT_TOOL_DENIED_PATTERNS", raising=False)
    else:
        monkeypatch.setenv("MONKEYBOT_TOOL_DENIED_PATTERNS", denied)
    runtime = GatewayRuntime()
    runtime.build_inspectors(_layout(tmp_path, command_allowlist=command_allowlist))
    return runtime


def test_build_inspectors_missing_tiers_allows_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _build(tmp_path, monkeypatch, command_allowlist=tmp_path / "missing.yaml")
    assert runtime.run_command_allowed_commands is None
    assert runtime.run_command_allowed_path_prefixes is None
    assert not any(isinstance(i, CommandTierInspector) for i in runtime.inspectors)
    assert any(isinstance(i, LoopStartInspector) for i in runtime.inspectors)
    assert runtime.computer_tools == []
    assert runtime.computer_approvals_persist is None


def test_build_inspectors_denied_patterns_adds_rules_inspector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _build(
        tmp_path,
        monkeypatch,
        command_allowlist=tmp_path / "missing.yaml",
        denied="rm -rf,DROP TABLE",
    )
    rules = [i for i in runtime.inspectors if isinstance(i, RulesInspector)]
    assert len(rules) == 1
    assert rules[0].denied_patterns == ["rm -rf", "DROP TABLE"]


def test_build_inspectors_empty_denied_patterns_skips_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _build(
        tmp_path,
        monkeypatch,
        command_allowlist=tmp_path / "missing.yaml",
        denied="",
    )
    assert not any(isinstance(i, RulesInspector) for i in runtime.inspectors)


def test_build_inspectors_tiers_file_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = tmp_path / "command_allowlist.yaml"
    policy.write_text("allowed_commands:\n  - echo\n", encoding="utf-8")
    runtime = _build(tmp_path, monkeypatch, command_allowlist=policy, denied="")
    tiers = [i for i in runtime.inspectors if isinstance(i, CommandTierInspector)]
    assert len(tiers) == 1
    assert runtime.run_command_allowed_commands == ["echo"]
