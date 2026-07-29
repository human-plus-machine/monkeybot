"""Tests for the ! local-shell helpers."""

from __future__ import annotations

import sys
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
