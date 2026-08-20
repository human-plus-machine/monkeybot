"""Tests for ``monkeybot run`` agent-root resolution."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from monkeybot_cli.commands.run_cmd import run_run
from monkeybot_cli.runtime_python import RuntimePython


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

    def fake_run_gateway(cmd, *, env, cwd):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return 0

    args = argparse.Namespace(cwd=None, config=str(cfg_dir / "monkeybot.yaml"), port=None)
    runtime = RuntimePython([str(venv_py.resolve())], "venv", agent)

    with (
        patch("monkeybot_cli.commands.run_cmd.prepare_runtime_python", return_value=runtime),
        patch("monkeybot_cli.commands.run_cmd.run_gateway_process", side_effect=fake_run_gateway),
    ):
        code = run_run(args)

    assert code == 0
    # Gateway launched from the agent root derived from --config, not the shell cwd.
    assert Path(captured["cwd"]).resolve() == agent.resolve()
    assert captured["cmd"][0] == str(venv_py.resolve())


def test_run_run_preserves_explicit_cwd_with_explicit_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    def fake_run_gateway(cmd, *, env, cwd):  # type: ignore[no-untyped-def]
        captured["cwd"] = cwd
        return 0

    args = argparse.Namespace(cwd=str(explicit_cwd), config=str(cfg_dir / "monkeybot.yaml"), port=None)
    runtime = RuntimePython([sys.executable], "cli", explicit_cwd)

    with (
        patch("monkeybot_cli.commands.run_cmd.prepare_runtime_python", return_value=runtime),
        patch("monkeybot_cli.commands.run_cmd.run_gateway_process", side_effect=fake_run_gateway),
    ):
        run_run(args)

    assert Path(captured["cwd"]).resolve() == explicit_cwd.resolve()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal forwarding")
def test_run_gateway_process_forwards_sigterm() -> None:
    """SIGTERM on the CLI shim must reach uvicorn, not just kill the shim."""
    inner = (
        "import signal, sys, time;"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(42));"
        "time.sleep(30)"
    )
    wrapper_src = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from monkeybot_cli.commands.run_cmd import run_gateway_process\n"
        f"raise SystemExit(run_gateway_process([sys.executable, '-c', {inner!r}], "
        "env=os.environ.copy(), cwd=Path('.')))\n"
    )
    wrapper = subprocess.Popen([sys.executable, "-c", wrapper_src], env=os.environ.copy())
    try:
        time.sleep(0.4)
        assert wrapper.poll() is None
        wrapper.send_signal(signal.SIGTERM)
        assert wrapper.wait(timeout=8) == 42
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=5)
