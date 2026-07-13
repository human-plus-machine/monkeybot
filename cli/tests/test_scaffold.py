"""Tests for CLI scaffolding (``monkeybot_cli.scaffold``)."""

from __future__ import annotations

from pathlib import Path

from monkeybot_cli.scaffold import run_new


def test_run_new_creates_bundle(tmp_path: Path) -> None:
    run_new(dest=tmp_path, force=False)
    cfg = tmp_path / "monkeybot_config"
    assert (cfg / "monkeybot.yaml").is_file()
    assert (cfg / "monkeybot.example.yaml").is_file()
    assert (cfg / "mcp.json").read_text(encoding="utf-8").strip().startswith("{")
    assert (cfg / "command_allowlist.yaml").is_file()
    assert (cfg / "permissions.yaml").is_file()
    assert (cfg / "AGENT.md").is_file()
    assert (cfg / "otel-collector.example.yaml").is_file()
    assert (cfg / "env.example").is_file()
    assert (tmp_path / ".env.example").is_file()
    assert (tmp_path / "data" / "memory" / "INDEX.md").is_file()
    assert (tmp_path / "skills").is_dir()
    assert (tmp_path / "workspace" / ".gitkeep").is_file()
    assert (tmp_path / "scripts" / "setup-workspace.sh").is_file()


def test_run_new_skips_existing(tmp_path: Path) -> None:
    run_new(dest=tmp_path, force=False)
    mtime = (tmp_path / "monkeybot_config" / "AGENT.md").stat().st_mtime_ns
    run_new(dest=tmp_path, force=False)
    assert (tmp_path / "monkeybot_config" / "AGENT.md").stat().st_mtime_ns == mtime


def test_run_new_force_overwrites(tmp_path: Path) -> None:
    run_new(dest=tmp_path, force=False)
    p = tmp_path / "monkeybot_config" / "AGENT.md"
    p.write_text("custom\n", encoding="utf-8")
    run_new(dest=tmp_path, force=True)
    text = p.read_text(encoding="utf-8")
    assert "Making files and code changes" in text
    assert "custom" not in text
