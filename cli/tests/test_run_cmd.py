"""Tests for ``monkeybot run`` agent-root resolution."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from monkeybot_cli.commands.run_cmd import run_run


def test_run_run_derives_agent_root_from_off_tree_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = tmp_path / "agent"
    cfg_dir = agent / "monkeybot_config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "monkeybot.yaml").write_text(
        "model:\n  provider: gemini\n  name: test\n", encoding="utf-8"
    )
    # Agent-local venv so resolve_runtime_python selects the "venv" source.
    venv_bin = agent / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_py = venv_bin / "python"
    venv_py.write_text("stub", encoding="utf-8")

    monkeypatch.chdir(tmp_path)  # no .env here, so load_agent_dotenv is a no-op

    captured: dict[str, object] = {}

    class _Proc:
        returncode = 0

    def fake_run(cmd, env=None, cwd=None):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return _Proc()

    args = argparse.Namespace(cwd=None, config=str(cfg_dir / "monkeybot.yaml"), port=None)

    with patch("monkeybot_cli.commands.run_cmd.subprocess.run", side_effect=fake_run):
        code = run_run(args)

    assert code == 0
    # Gateway launched from the agent root derived from --config, not the shell cwd.
    assert Path(captured["cwd"]).resolve() == agent.resolve()
    assert captured["cmd"][0] == str(venv_py.resolve())


def test_run_run_uses_explicit_config_agent_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = tmp_path / "agent"
    cfg_dir = agent / "monkeybot_config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "monkeybot.yaml").write_text(
        "model:\n  provider: gemini\n  name: test\n", encoding="utf-8"
    )
    explicit_cwd = tmp_path / "elsewhere"
    explicit_cwd.mkdir()

    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}

    class _Proc:
        returncode = 0

    def fake_run(cmd, env=None, cwd=None):  # type: ignore[no-untyped-def]
        captured["cwd"] = cwd
        return _Proc()

    args = argparse.Namespace(cwd=str(explicit_cwd), config=str(cfg_dir / "monkeybot.yaml"), port=None)

    with patch("monkeybot_cli.commands.run_cmd.subprocess.run", side_effect=fake_run):
        run_run(args)

    # An explicit config selects its own agent root; ``--cwd`` only participates
    # in config discovery when no explicit config was supplied.
    assert Path(captured["cwd"]).resolve() == agent.resolve()
