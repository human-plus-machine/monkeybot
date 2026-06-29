"""Tests for ``monkeybot.cli.init_config``."""

from __future__ import annotations

from pathlib import Path

from monkeybot.cli.init_config import main, run_init


def test_init_config_creates_bundle(tmp_path: Path) -> None:
    assert main(["--dest", str(tmp_path)]) == 0
    cfg = tmp_path / "monkeybot_config"
    assert (cfg / "monkeybot.yaml").is_file()
    assert (cfg / "monkeybot.example.yaml").is_file()
    assert (cfg / "mcp.json").read_text(encoding="utf-8").strip().startswith("{")
    assert (cfg / "command_allowlist.yaml").is_file()
    assert (cfg / "AGENT.md").is_file()
    assert (tmp_path / "data" / "memory" / "INDEX.md").is_file()
    assert (tmp_path / "skills").is_dir()


def test_init_config_skips_existing(tmp_path: Path) -> None:
    assert run_init(dest=tmp_path, force=False) == 0
    mtime = (tmp_path / "monkeybot_config" / "AGENT.md").stat().st_mtime_ns
    assert run_init(dest=tmp_path, force=False) == 0
    assert (tmp_path / "monkeybot_config" / "AGENT.md").stat().st_mtime_ns == mtime


def test_init_config_force_overwrites(tmp_path: Path) -> None:
    assert run_init(dest=tmp_path, force=False) == 0
    p = tmp_path / "monkeybot_config" / "AGENT.md"
    p.write_text("custom\n", encoding="utf-8")
    assert run_init(dest=tmp_path, force=True) == 0
    assert "Replace this file" in p.read_text(encoding="utf-8")


def test_init_config_rejects_non_dir(tmp_path: Path) -> None:
    f = tmp_path / "not_a_dir"
    f.write_text("x", encoding="utf-8")
    assert main(["--dest", str(f)]) == 2
