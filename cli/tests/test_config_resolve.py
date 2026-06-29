"""Tests for CLI config path resolution."""

from __future__ import annotations

from pathlib import Path

from monkeybot_cli.commands.chat import _is_exit_command
from monkeybot_cli.config_resolve import resolve_config


def test_resolve_config_uses_cwd_without_chdir(tmp_path: Path, monkeypatch) -> None:
    other = tmp_path / "agent"
    cfg_dir = other / "monkeybot_config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "monkeybot.yaml").write_text("model:\n  provider: gemini\n  name: test\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)

    resolved = resolve_config(None, cwd=other)

    assert resolved == (cfg_dir / "monkeybot.yaml").resolve()
    assert Path.cwd() == tmp_path


def test_is_exit_command_requires_slash_prefix() -> None:
    assert _is_exit_command("/bye")
    assert _is_exit_command("/quit")
    assert not _is_exit_command("bye")
    assert not _is_exit_command("quit")
    assert not _is_exit_command("exit")
