"""Tests for the ! local-shell helpers."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from monkeybot_cli.chat_local_shell import run_local_shell, truncate_output


def test_run_local_shell_echo(tmp_path: Path) -> None:
    out, code = run_local_shell("echo hi", tmp_path)
    assert out.strip() == "hi"
    assert code == 0


def test_run_local_shell_nonzero_exit(tmp_path: Path) -> None:
    out, code = run_local_shell("exit 3", tmp_path)
    assert code == 3


def test_run_local_shell_uses_cwd(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("x")
    out, code = run_local_shell("ls", tmp_path)
    assert "marker.txt" in out
    assert code == 0


def test_run_local_shell_timeout(tmp_path: Path) -> None:
    out, code = run_local_shell(f"{sys.executable} -c 'import time; time.sleep(5)'", tmp_path, timeout=0.2)
    assert code is None
    assert "timed out" in out


def test_run_local_shell_timeout_kills_grandchildren(tmp_path: Path) -> None:
    """A timed-out command must not leave children it spawned still running.

    Regression test: plain `subprocess.run(..., timeout=...)` only kills the
    shell itself, so a backgrounded grandchild (e.g. `sleep 300 &`) survives
    past the reported timeout. run_local_shell must kill the whole process
    group instead.
    """
    pid_file = tmp_path / "child.pid"
    child_script = (
        f"import pathlib, os, time; "
        f"pathlib.Path(r'{pid_file}').write_text(str(os.getpid())); "
        f"time.sleep(5)"
    )
    command = f'{sys.executable} -c "{child_script}" & sleep 5'
    out, code = run_local_shell(command, tmp_path, timeout=0.3)
    assert code is None
    assert "timed out" in out

    deadline = time.monotonic() + 2.0
    child_pid: int | None = None
    while time.monotonic() < deadline and child_pid is None:
        if pid_file.exists():
            child_pid = int(pid_file.read_text().strip())
        else:
            time.sleep(0.05)
    assert child_pid is not None, "grandchild never started"

    # Give the kill signal a moment to land, then confirm the grandchild
    # didn't survive the shell's death.
    deadline = time.monotonic() + 2.0
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.05)
    assert not alive, "grandchild process outlived the timed-out shell"


def test_truncate_output_lines() -> None:
    text = "\n".join(f"line{i}" for i in range(300))
    result = truncate_output(text, max_lines=10, max_chars=100_000)
    assert result.count("\n") == 10
    assert "+290 lines truncated" in result


def test_truncate_output_chars() -> None:
    text = "x" * 500
    result = truncate_output(text, max_lines=1000, max_chars=100)
    assert len(result) < 500
    assert "chars truncated" in result


def test_truncate_output_short_text_unchanged() -> None:
    assert truncate_output("hello") == "hello"
