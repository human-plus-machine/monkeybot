"""Tests for the ! local-shell helpers."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import monkeybot_cli.chat_local_shell as chat_local_shell
import monkeybot_cli.process_tree as process_tree
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


def test_kill_process_tree_swallows_permission_error(monkeypatch) -> None:
    monkeypatch.setattr(process_tree, "IS_WINDOWS", False)

    def _boom_killpg(pid: int, sig: int) -> None:
        raise PermissionError("race")

    def _boom_kill(pid: int, sig: int) -> None:
        raise ProcessLookupError()

    monkeypatch.setattr(process_tree.os, "killpg", _boom_killpg)
    monkeypatch.setattr(process_tree.os, "kill", _boom_kill)
    process_tree.kill_process_tree(4321)  # must not raise


def test_run_local_shell_timeout(tmp_path: Path) -> None:
    out, code = run_local_shell(f"{sys.executable} -c 'import time; time.sleep(5)'", tmp_path, timeout=0.2)
    assert code is None
    assert "timed out" in out


def test_run_local_shell_caps_unbounded_output(tmp_path: Path) -> None:
    script = tmp_path / "flood.py"
    script.write_text(
        "import sys\nwhile True:\n    sys.stdout.write('y' * 4096)\n    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    out, code = run_local_shell(f"{sys.executable} {script}", tmp_path, timeout=5.0)
    assert code is not None
    assert len(out) <= chat_local_shell._MAX_CAPTURE_CHARS + 80
    assert "output capped" in out


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


def test_popen_kwargs_use_process_group_on_posix(monkeypatch) -> None:
    monkeypatch.setattr(process_tree, "IS_WINDOWS", False)
    assert process_tree.popen_kwargs_for_platform() == {"start_new_session": True}


def test_popen_kwargs_use_new_process_group_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(process_tree, "IS_WINDOWS", True)
    kwargs = process_tree.popen_kwargs_for_platform()
    assert set(kwargs) == {"creationflags"}


def test_kill_process_tree_uses_killpg_on_posix(monkeypatch) -> None:
    monkeypatch.setattr(process_tree, "IS_WINDOWS", False)
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(process_tree.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    process_tree.kill_process_tree(4321)
    assert calls == [(4321, process_tree.signal.SIGKILL)]


def test_kill_process_tree_uses_taskkill_on_windows(monkeypatch) -> None:
    """Regression test for the AttributeError os.killpg raised on Windows.

    os.killpg doesn't exist on Windows, so the POSIX-only kill path would
    crash a timed-out `!` command instead of finishing the tool block there.
    """
    monkeypatch.setattr(process_tree, "IS_WINDOWS", True)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        process_tree.subprocess,
        "run",
        lambda argv, **kwargs: calls.append(list(argv)),
    )
    process_tree.kill_process_tree(4321)
    assert calls == [["taskkill", "/F", "/T", "/PID", "4321"]]


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
