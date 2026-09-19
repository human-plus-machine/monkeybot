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
        grants_path=tmp_path / "grants.json",
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
        "monkeybot.gateway.sse.app.should_enable_computer_tools", lambda _cfg=None: False
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


def test_build_verifier_wires_ledger_tracker_judge_and_inspector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.config import apply_monkeybot_runtime_env, get_config_store
    from monkeybot.core.config.runtime_env import reset_runtime_env_state_for_tests
    from monkeybot.core.persistence.goal_ledger import InMemoryGoalLedgerStore
    from monkeybot.core.verifier.inspector import VerifierInspector
    from monkeybot.core.verifier.judge import ProviderJudge

    class _Storage:
        def __init__(self) -> None:
            self._store = InMemoryGoalLedgerStore()

        def goal_ledger(self) -> InMemoryGoalLedgerStore:
            return self._store

    monkeypatch.chdir(tmp_path)
    reset_runtime_env_state_for_tests()
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    yaml_path = cfg_dir / "monkeybot.yaml"
    yaml_path.write_text(
        "model:\n  provider: fake\n  name: glm-5.3-flash\n"
        "verifier:\n  enabled: true\n"
        "  ledger:\n    enabled: true\n"
        "  tracker:\n    enabled: true\n"
        "  judge:\n    enabled: true\n"
        "  escalation:\n    max_severity: nudge\n",
        encoding="utf-8",
    )
    apply_monkeybot_runtime_env(config_path=yaml_path, agent_root=tmp_path)
    runtime = GatewayRuntime()
    try:
        runtime.build_verifier(get_config_store().current(), storage=_Storage())
        assert runtime.goal_ledger is not None
        assert runtime.progress_tracker is not None
        assert runtime.verdict_mailbox is not None
        assert runtime.judge_worker is not None
        assert isinstance(runtime.judge_worker._port, ProviderJudge)
        assert any(isinstance(i, VerifierInspector) for i in runtime.inspectors)
        assert runtime._live_judge_model(get_config_store().current()) == "glm-5.3-flash"
        runtime.build_verifier(get_config_store().current(), storage=_Storage())
    finally:
        if runtime.judge_worker is not None:
            runtime.judge_worker.close()
        reset_runtime_env_state_for_tests()


def test_build_verifier_parent_enabled_implies_nested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.config import apply_monkeybot_runtime_env, get_config_store
    from monkeybot.core.config.runtime_env import reset_runtime_env_state_for_tests
    from monkeybot.core.persistence.goal_ledger import InMemoryGoalLedgerStore
    from monkeybot.core.verifier.judge import ProviderJudge

    class _Storage:
        def __init__(self) -> None:
            self._store = InMemoryGoalLedgerStore()

        def goal_ledger(self) -> InMemoryGoalLedgerStore:
            return self._store

    monkeypatch.chdir(tmp_path)
    reset_runtime_env_state_for_tests()
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    yaml_path = cfg_dir / "monkeybot.yaml"
    yaml_path.write_text(
        "model:\n  provider: fake\n  name: glm-5.3-flash\nverifier:\n  enabled: true\n",
        encoding="utf-8",
    )
    apply_monkeybot_runtime_env(config_path=yaml_path, agent_root=tmp_path)
    runtime = GatewayRuntime()
    try:
        runtime.build_verifier(get_config_store().current(), storage=_Storage())
        assert runtime.goal_ledger is not None
        assert runtime.progress_tracker is not None
        assert runtime.judge_worker is not None
        assert isinstance(runtime.judge_worker._port, ProviderJudge)
    finally:
        if runtime.judge_worker is not None:
            runtime.judge_worker.close()
        reset_runtime_env_state_for_tests()
