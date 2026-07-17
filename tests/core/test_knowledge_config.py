"""Tests for knowledge settings path resolution (F20)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from monkeybot.core.config import runtime_env
from monkeybot.core.knowledge.config import resolve_knowledge_settings


@pytest.fixture(autouse=True)
def _clean_knowledge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "KNOWLEDGE_LOCAL_INDEX_PATH",
        "KNOWLEDGE_ENABLED",
        "KNOWLEDGE_EMBEDDINGS_ENABLED",
        "MONKEYBOT_CONFIG",
    ):
        monkeypatch.delenv(key, raising=False)
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
        "  local_index:\n"
        "    path: .monkeybot/knowledge/index.sqlite\n"
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


def test_absolute_index_env_overrides_workspace_anchor(
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

    settings = resolve_knowledge_settings(
        agent_root=agent,
        config_path=cfg / "monkeybot.yaml",
        workspace_root=workspace,
    )
    assert settings.index_path == str(absolute.resolve())


def test_runtime_env_leaves_knowledge_index_path_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "knowledge:\n"
        "  local_index:\n"
        "    path: .monkeybot/knowledge/index.sqlite\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("KNOWLEDGE_LOCAL_INDEX_PATH", raising=False)
    runtime_env.apply_monkeybot_runtime_env(agent_root=tmp_path)
    # Must stay relative so resolve_knowledge_settings can anchor to workspace
    assert os.environ.get("KNOWLEDGE_LOCAL_INDEX_PATH") == (
        ".monkeybot/knowledge/index.sqlite"
    )


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
