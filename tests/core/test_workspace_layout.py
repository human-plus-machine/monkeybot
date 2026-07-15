"""Tests for portable workspace root resolution from monkeybot.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from monkeybot.core.workspace_layout import resolve_agent_workspace_root


def _clear_agent_root_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MONKEYBOT_AGENT_ROOT", raising=False)
    monkeypatch.delenv("MONKEYBOT_CONFIG", raising=False)


def _write_config(agent_root: Path, *, workspace_root: str | None = None) -> Path:
    cfg_dir = agent_root / "monkeybot_config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    doc: dict = {"paths": {}}
    if workspace_root is not None:
        doc["paths"]["workspace_root"] = workspace_root
    cfg = cfg_dir / "monkeybot.yaml"
    cfg.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return cfg


def test_resolve_prefers_yaml_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "custom-ws"
    ws.mkdir()
    _write_config(tmp_path, workspace_root="./custom-ws")
    monkeypatch.chdir(tmp_path)
    _clear_agent_root_overrides(monkeypatch)
    # Env must not win over yaml.
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_ROOT", str(tmp_path / "env-ws"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "legacy-ws"))
    assert resolve_agent_workspace_root() == ws.resolve()


def test_resolve_ignores_env_workspace_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "workspace"
    nested.mkdir()
    _write_config(tmp_path)  # no paths.workspace_root → default workspace/
    monkeypatch.chdir(tmp_path)
    _clear_agent_root_overrides(monkeypatch)
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_ROOT", str(tmp_path / "env-ws"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "legacy-ws"))
    assert resolve_agent_workspace_root() == nested.resolve()


def test_resolve_nested_workspace_when_no_yaml_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "workspace"
    nested.mkdir()
    monkeypatch.chdir(tmp_path)
    _clear_agent_root_overrides(monkeypatch)
    monkeypatch.delenv("MONKEYBOT_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    assert resolve_agent_workspace_root() == nested.resolve()


def test_resolve_agent_root_workspace_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _clear_agent_root_overrides(monkeypatch)
    monkeypatch.delenv("MONKEYBOT_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    assert resolve_agent_workspace_root() == (tmp_path / "workspace").resolve()
