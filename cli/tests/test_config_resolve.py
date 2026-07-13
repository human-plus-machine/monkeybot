"""Tests for CLI config path resolution."""

from __future__ import annotations

from pathlib import Path

from monkeybot_cli.config_resolve import resolve_agent_root, resolve_config


def test_resolve_config_uses_cwd_without_chdir(tmp_path: Path, monkeypatch) -> None:
    other = tmp_path / "agent"
    cfg_dir = other / "monkeybot_config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "monkeybot.yaml").write_text("model:\n  provider: gemini\n  name: test\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)

    resolved = resolve_config(None, cwd=other)

    assert resolved == (cfg_dir / "monkeybot.yaml").resolve()
    assert Path.cwd() == tmp_path


def test_explicit_cwd_does_not_walk_to_a_parent_agent(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    (agent / "monkeybot_config").mkdir(parents=True)
    (agent / "monkeybot_config" / "monkeybot.yaml").write_text("model: {}\n", encoding="utf-8")
    nested = agent / "workspace" / "deep"
    nested.mkdir(parents=True)

    assert resolve_config(None, cwd=nested) is None
    assert resolve_agent_root(cwd=nested) == nested.resolve()
