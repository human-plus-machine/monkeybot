"""Tests for CLI scaffolding (``monkeybot_cli.scaffold``)."""

from __future__ import annotations

from pathlib import Path

from monkeybot_cli.compat import COMPATIBLE_CORE_RANGE
from monkeybot_cli.scaffold import monkeybot_dep_for_provider, run_new, write_agent_pyproject


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
    assert (tmp_path / "memory" / "INDEX.md").is_file()
    assert (tmp_path / "workspace" / "skills").is_dir()
    assert not (tmp_path / "workspace" / "skills").is_symlink()
    assert not (tmp_path / "skills").exists()
    assert (tmp_path / "workspace").is_dir()
    assert (tmp_path / "scripts" / "setup-workspace.sh").is_file()
    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert f"monkeybot[gemini]{COMPATIBLE_CORE_RANGE}" in pyproject
    assert "[tool.uv.sources]" not in pyproject


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


def test_write_agent_pyproject_maps_provider_extra(tmp_path: Path) -> None:
    dest = tmp_path / "My Cool Bot"
    dest.mkdir()
    status = write_agent_pyproject(dest, provider="anthropic", force=False)
    assert status == "created"
    text = (dest / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "my-cool-bot"' in text
    assert f'"monkeybot[claude]{COMPATIBLE_CORE_RANGE}"' in text
    assert "[tool.uv.sources]" not in text


def test_write_agent_pyproject_merges_feature_extras(tmp_path: Path) -> None:
    write_agent_pyproject(
        tmp_path,
        provider="openai",
        extras=["postgres", "sandbox", "observability"],
        force=False,
    )
    text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert (
        f'"monkeybot[openai,postgres,sandbox,observability]{COMPATIBLE_CORE_RANGE}"'
        in text
    )


def test_write_agent_pyproject_fake_has_no_extra(tmp_path: Path) -> None:
    status = write_agent_pyproject(tmp_path, provider="fake", force=False)
    assert status == "created"
    text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"monkeybot{COMPATIBLE_CORE_RANGE}"' in text
    assert "monkeybot[" not in text


def test_write_agent_pyproject_fake_with_features(tmp_path: Path) -> None:
    write_agent_pyproject(tmp_path, provider="fake", extras=["postgres"], force=False)
    text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"monkeybot[postgres]{COMPATIBLE_CORE_RANGE}"' in text


def test_write_agent_pyproject_skips_without_force(tmp_path: Path) -> None:
    write_agent_pyproject(tmp_path, provider="openai", force=False)
    (tmp_path / "pyproject.toml").write_text("custom\n", encoding="utf-8")
    assert write_agent_pyproject(tmp_path, provider="openai", force=False) == "skipped"
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == "custom\n"


def test_write_agent_pyproject_force_overwrites(tmp_path: Path) -> None:
    write_agent_pyproject(tmp_path, provider="openai", force=False)
    status = write_agent_pyproject(tmp_path, provider="aws_bedrock", force=True)
    assert status == "overwritten"
    text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"monkeybot[bedrock]{COMPATIBLE_CORE_RANGE}"' in text


def test_monkeybot_dep_for_provider_aliases() -> None:
    assert monkeybot_dep_for_provider("openai") == f"monkeybot[openai]{COMPATIBLE_CORE_RANGE}"
    assert monkeybot_dep_for_provider("anthropic") == f"monkeybot[claude]{COMPATIBLE_CORE_RANGE}"
    assert monkeybot_dep_for_provider("aws_bedrock") == f"monkeybot[bedrock]{COMPATIBLE_CORE_RANGE}"
    assert monkeybot_dep_for_provider(None) == f"monkeybot[gemini]{COMPATIBLE_CORE_RANGE}"
    assert monkeybot_dep_for_provider("fake") == f"monkeybot{COMPATIBLE_CORE_RANGE}"


def test_monkeybot_requirement_dedupes_provider_in_extras() -> None:
    from monkeybot_cli.scaffold import monkeybot_requirement

    dep = monkeybot_requirement(provider="openai", extras=["openai", "postgres"])
    assert dep == f"monkeybot[openai,postgres]{COMPATIBLE_CORE_RANGE}"
