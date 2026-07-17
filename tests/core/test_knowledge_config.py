"""Tests for knowledge settings path resolution (YAML only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.config import runtime_env
from monkeybot.core.knowledge.config import (
    knowledge_enabled_from_config,
    resolve_knowledge_settings,
)


@pytest.fixture(autouse=True)
def _reset_runtime_env() -> None:
    runtime_env.reset_runtime_env_state_for_tests()
    yield
    runtime_env.reset_runtime_env_state_for_tests()


def test_knowledge_paths_anchor_to_workspace(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    workspace = agent / "workspace"
    workspace.mkdir(parents=True)
    cfg = agent / "monkeybot_config"
    cfg.mkdir()
    (cfg / "monkeybot.yaml").write_text(
        "knowledge:\n"
        "  enabled: true\n"
        "  store:\n"
        "    path: .monkeybot/knowledge/vectors.sqlite\n",
        encoding="utf-8",
    )

    settings = resolve_knowledge_settings(
        agent_root=agent,
        config_path=cfg / "monkeybot.yaml",
        workspace_root=workspace,
    )
    assert settings.index_path == str(
        (workspace / ".monkeybot" / "knowledge" / "index.sqlite").resolve()
    )
    assert settings.store.path == str(
        (workspace / ".monkeybot" / "knowledge" / "vectors.sqlite").resolve()
    )
    assert settings.knowledge_root == str(
        (workspace / ".monkeybot" / "knowledge").resolve()
    )
    assert settings.debounce_ms == 300
    assert settings.startup_scan is True
    assert settings.max_file_bytes == 5_000_000


def test_env_does_not_override_index_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = tmp_path / "agent"
    workspace = agent / "workspace"
    workspace.mkdir(parents=True)
    cfg = agent / "monkeybot_config"
    cfg.mkdir()
    (cfg / "monkeybot.yaml").write_text("knowledge:\n  enabled: true\n", encoding="utf-8")
    absolute = tmp_path / "custom" / "index.sqlite"
    monkeypatch.setenv("KNOWLEDGE_LOCAL_INDEX_PATH", str(absolute))
    monkeypatch.setenv("KNOWLEDGE_ENABLED", "false")

    settings = resolve_knowledge_settings(
        agent_root=agent,
        config_path=cfg / "monkeybot.yaml",
        workspace_root=workspace,
    )
    assert settings.index_path == str(
        (workspace / ".monkeybot" / "knowledge" / "index.sqlite").resolve()
    )
    assert settings.enabled is True
    assert knowledge_enabled_from_config(str(cfg / "monkeybot.yaml")) is True


def test_runtime_env_does_not_export_knowledge_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "knowledge:\n"
        "  enabled: true\n"
        "  search:\n"
        "    default_limit: 12\n",
        encoding="utf-8",
    )
    for key in (
        "KNOWLEDGE_ENABLED",
        "KNOWLEDGE_LOCAL_INDEX_PATH",
        "KNOWLEDGE_SEARCH_DEFAULT_LIMIT",
        "KNOWLEDGE_EMBEDDINGS_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    runtime_env.apply_monkeybot_runtime_env(agent_root=tmp_path)
    assert "KNOWLEDGE_ENABLED" not in __import__("os").environ
    assert "KNOWLEDGE_LOCAL_INDEX_PATH" not in __import__("os").environ
    assert "KNOWLEDGE_SEARCH_DEFAULT_LIMIT" not in __import__("os").environ


def test_migrates_legacy_agent_root_index(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    workspace = agent / "workspace"
    workspace.mkdir(parents=True)
    legacy_dir = agent / ".monkeybot" / "knowledge"
    legacy_dir.mkdir(parents=True)
    legacy_index = legacy_dir / "index.sqlite"
    legacy_index.write_bytes(b"sqlite-bytes")
    cfg = agent / "monkeybot_config"
    cfg.mkdir()
    (cfg / "monkeybot.yaml").write_text("knowledge:\n  enabled: true\n", encoding="utf-8")

    settings = resolve_knowledge_settings(
        agent_root=agent,
        config_path=cfg / "monkeybot.yaml",
        workspace_root=workspace,
    )
    dest = Path(settings.index_path)
    assert dest.is_file()
    assert dest.read_bytes() == b"sqlite-bytes"
    assert not legacy_index.is_file()
