"""Tests for the ``approvals_path`` field added to ``AgentLayout`` for the
``computer_*`` tools' durable "Always allow" overlay."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from monkeybot.core.layout import AgentLayout


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("MONKEYBOT_APPROVALS_CONFIG", "PERMISSION_CONFIG", "MONKEYBOT_CONFIG"):
        monkeypatch.delenv(key, raising=False)


def _make_agent_root(tmp_path: Path) -> Path:
    root = tmp_path / "agent"
    (root / "monkeybot_config").mkdir(parents=True)
    return root


def test_default_approvals_path(tmp_path: Path) -> None:
    root = _make_agent_root(tmp_path)
    layout = AgentLayout.from_environment(agent_root=root)
    assert layout.approvals_path == root / "monkeybot_config" / "approvals.json"


def test_approvals_path_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_agent_root(tmp_path)
    override = tmp_path / "elsewhere" / "approvals.json"
    monkeypatch.setenv("MONKEYBOT_APPROVALS_CONFIG", str(override))
    layout = AgentLayout.from_environment(agent_root=root)
    assert layout.approvals_path == override.resolve()


def test_export_environment_sets_approvals_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_agent_root(tmp_path)
    layout = AgentLayout.from_environment(agent_root=root)
    monkeypatch.delenv("MONKEYBOT_APPROVALS_CONFIG", raising=False)
    layout.export_environment()
    assert os.environ.get("MONKEYBOT_APPROVALS_CONFIG") == str(layout.approvals_path)
