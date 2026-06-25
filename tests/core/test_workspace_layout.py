"""Tests for portable workspace root resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.workspace_layout import resolve_agent_workspace_root


def test_resolve_prefers_monkeybot_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "custom-ws"
    ws.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_ROOT", str(ws))
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    assert resolve_agent_workspace_root() == ws.resolve()


def test_resolve_workspace_root_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "legacy-ws"
    ws.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MONKEYBOT_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
    assert resolve_agent_workspace_root() == ws.resolve()


def test_resolve_monkeybot_takes_precedence_over_workspace_root_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = tmp_path / "primary"
    legacy = tmp_path / "legacy"
    primary.mkdir()
    legacy.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_ROOT", str(primary))
    monkeypatch.setenv("WORKSPACE_ROOT", str(legacy))
    assert resolve_agent_workspace_root() == primary.resolve()


def test_resolve_nested_workspace_when_no_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "workspace"
    nested.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MONKEYBOT_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    assert resolve_agent_workspace_root() == nested.resolve()


def test_resolve_cwd_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MONKEYBOT_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    assert resolve_agent_workspace_root() == tmp_path.resolve()
