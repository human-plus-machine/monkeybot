"""Tests for ``monkeybot run`` agent-root resolution."""

from __future__ import annotations

import argparse
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from monkeybot_cli.commands.run_cmd import _cli_exit_status, run_run
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

    args = argparse.Namespace(
        cwd=str(explicit_cwd), config=str(cfg_dir / "monkeybot.yaml"), port=None
    )
    runtime = RuntimePython([sys.executable], "cli", explicit_cwd)

    with (
        patch("monkeybot_cli.commands.run_cmd.prepare_runtime_python", return_value=runtime),
        patch("monkeybot_cli.commands.run_cmd.run_gateway_process", side_effect=fake_run_gateway),
    ):
        run_run(args)

    assert Path(captured["cwd"]).resolve() == explicit_cwd.resolve()


def test_cli_exit_status_translates_signal_wait_codes() -> None:
    assert _cli_exit_status(-signal.SIGTERM, signal.SIGTERM) == 128 + signal.SIGTERM
    assert _cli_exit_status(-signal.SIGINT, signal.SIGINT) == 130
    assert _cli_exit_status(0, signal.SIGTERM) == 0
    assert _cli_exit_status(42, None) == 42
    assert _cli_exit_status(None, signal.SIGTERM) == 1


def _spawn_wrapper(
    inner_cmd: list[str], *, shutdown_timeout: float = 1.0
) -> subprocess.Popen[bytes]:
    wrapper_src = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from monkeybot_cli.commands.run_cmd import run_gateway_process\n"
        f"raise SystemExit(run_gateway_process({inner_cmd!r}, "
        "env=os.environ.copy(), cwd=Path('.'), "
        f"shutdown_timeout={shutdown_timeout}, kill_timeout=0.5))\n"
    )
    return subprocess.Popen(
        [sys.executable, "-u", "-c", wrapper_src],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )


def _wait_ready(proc: subprocess.Popen[bytes], *, timeout: float = 8.0) -> str:
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else b""
            raise AssertionError(
                f"wrapper exited {proc.returncode} before ready: {err.decode(errors='replace')!r}"
            )
        ready, _, _ = select.select([proc.stdout], [], [], 0.1)
        if not ready:
            continue
        chunk = os.read(proc.stdout.fileno(), 64)
        if not chunk:
            continue
        buf += chunk
        if b"\n" in buf:
            return buf.split(b"\n", 1)[0].decode()
    raise AssertionError("timed out waiting for ready marker")


def _ready_then_sleep(setup: str = "") -> str:
    """Install handlers *before* the ready marker so SIGTERM cannot race."""
    return (
        "import sys, time;" + setup + "sys.stdout.write('ready\\n');"
        "sys.stdout.flush();"
        "time.sleep(30)"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal forwarding")
def test_run_gateway_process_forwards_sigterm() -> None:
    """SIGTERM on the CLI shim must reach uvicorn, not just kill the shim."""
    inner = _ready_then_sleep(
        "import signal; signal.signal(signal.SIGTERM, lambda *_: sys.exit(42));"
    )
    wrapper = _spawn_wrapper([sys.executable, "-c", inner])
    try:
        assert _wait_ready(wrapper) == "ready"
        wrapper.send_signal(signal.SIGTERM)
        assert wrapper.wait(timeout=8) == 42
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal forwarding")
def test_run_gateway_process_sigterm_exit_status_is_143() -> None:
    inner = _ready_then_sleep()
    wrapper = _spawn_wrapper([sys.executable, "-c", inner])
    try:
        assert _wait_ready(wrapper) == "ready"
        wrapper.send_signal(signal.SIGTERM)
        assert wrapper.wait(timeout=8) == 128 + signal.SIGTERM
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal forwarding")
def test_run_gateway_process_kills_child_that_ignores_sigterm() -> None:
    inner = _ready_then_sleep("import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN);")
    wrapper = _spawn_wrapper([sys.executable, "-c", inner], shutdown_timeout=0.4)
    try:
        assert _wait_ready(wrapper) == "ready"
        wrapper.send_signal(signal.SIGTERM)
        assert wrapper.wait(timeout=8) == 128 + signal.SIGKILL
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal forwarding")
def test_run_gateway_process_sigterm_kills_grandchild() -> None:
    """``uv run python`` is a grandchild; SIGTERM must not orphan it."""
    leaf = (
        "import os, sys, time;"
        "sys.stdout.write(f'ready {os.getpid()}\\n');"
        "sys.stdout.flush();"
        "time.sleep(30)"
    )
    middle = f"import subprocess, sys; raise SystemExit(subprocess.call([sys.executable, '-c', {leaf!r}]))"
    wrapper = _spawn_wrapper([sys.executable, "-c", middle])
    grandchild_pid: int | None = None
    try:
        marker = _wait_ready(wrapper)
        assert marker.startswith("ready ")
        grandchild_pid = int(marker.split()[1])
        wrapper.send_signal(signal.SIGTERM)
        wrapper.wait(timeout=8)
        with pytest.raises(ProcessLookupError):
            os.kill(grandchild_pid, 0)
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=5)
        if grandchild_pid is not None:
            try:
                os.kill(grandchild_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
