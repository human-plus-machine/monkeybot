"""Tests for CLI scaffolding (``monkeybot_cli.scaffold``)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from monkeybot_cli.compat import COMPATIBLE_CORE_RANGE
from monkeybot_cli.scaffold import (
    monkeybot_dep_for_provider,
    run_new,
    run_refresh,
    write_agent_pyproject,
)


def test_run_new_creates_bundle(tmp_path: Path) -> None:
    run_new(dest=tmp_path, force=False)
    cfg = tmp_path / "monkeybot_config"
    assert (cfg / "monkeybot.yaml").is_file()
    assert (cfg / "monkeybot.example.yaml").is_file()
    assert "image: python:3.12" in (cfg / "monkeybot.example.yaml").read_text(encoding="utf-8")
    assert (cfg / "mcp.json").read_text(encoding="utf-8").strip().startswith("{")
    assert (cfg / "command_allowlist.yaml").is_file()
    assert (cfg / "permissions.yaml").is_file()
    assert (cfg / "AGENT.md").is_file()
    assert (cfg / "otel-collector.example.yaml").is_file()
    assert not (cfg / "env.example").exists()
    assert (tmp_path / ".env.example").is_file()
    assert (tmp_path / "memory" / "mempalace" / "identity.txt").is_file()
    assert (tmp_path / "skills").is_dir()
    assert not (tmp_path / "skills" / "browser").exists()
    assert not (tmp_path / "skills" / "image-generator").exists()
    assert not (tmp_path / "skills" / "loop").exists()
    assert (tmp_path / "workspace" / ".gitkeep").is_file()
    assert (tmp_path / "workspace" / "browser" / "playbooks").is_dir()
    assert (tmp_path / "workspace" / "generated-media" / "images").is_dir()
    assert not (tmp_path / "workspace" / "skills").exists()
    assert (tmp_path / "Dockerfile").is_file()
    assert (tmp_path / ".dockerignore").is_file()
    assert (cfg / "opensandbox.docker.toml").is_file()
    yaml_text = (cfg / "monkeybot.yaml").read_text(encoding="utf-8")
    assert "localhost:18080" in yaml_text
    assert "Opt-in: requires Docker Desktop" in yaml_text
    assert "  enabled: false\n  # Keep off the gateway port" in yaml_text
    assert "read_max_lines: 5000" in yaml_text
    assert "2000 lines" in yaml_text
    assert "read_default_lines, spill_min_chars, spill_read_max_lines" in yaml_text
    assert "drives soft-spill / read_file char budgets" in yaml_text
    mcp = (cfg / "mcp.json").read_text(encoding="utf-8")
    assert '"browser"' in mcp and '"enabled": true' in mcp
    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert f"monkeybot[gemini,sandbox,web-search]{COMPATIBLE_CORE_RANGE}" in pyproject
    assert '"monkeybot-browser-mcp>=0.2.0,<1"' in pyproject
    assert "[tool.uv]\npackage = false" in pyproject
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
    assert f'"monkeybot[claude,sandbox,web-search]{COMPATIBLE_CORE_RANGE}"' in text
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
        f'"monkeybot[openai,sandbox,web-search,postgres,observability]{COMPATIBLE_CORE_RANGE}"'
        in text
    )


def test_write_agent_pyproject_fake_includes_sandbox(tmp_path: Path) -> None:
    status = write_agent_pyproject(tmp_path, provider="fake", force=False)
    assert status == "created"
    text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"monkeybot[sandbox,web-search]{COMPATIBLE_CORE_RANGE}"' in text


def test_write_agent_pyproject_fake_with_features(tmp_path: Path) -> None:
    write_agent_pyproject(tmp_path, provider="fake", extras=["postgres"], force=False)
    text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"monkeybot[sandbox,web-search,postgres]{COMPATIBLE_CORE_RANGE}"' in text


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
    assert f'"monkeybot[bedrock,sandbox,web-search]{COMPATIBLE_CORE_RANGE}"' in text


def test_run_refresh_adds_template_commands_and_keeps_extras(tmp_path: Path) -> None:
    run_new(dest=tmp_path, force=False)
    allow = tmp_path / "monkeybot_config" / "command_allowlist.yaml"
    allow.write_text(
        "allowed_commands:\n"
        "  - cat\n"
        "  - bash\n"
        "  - officecli\n"
        "allowed_path_prefixes:\n"
        "  - ./skills/\n"
        "  - ./custom-data/\n"
        "deny_patterns:\n"
        '  - "^sudo\\\\s+"\n'
        '  - "my-custom-deny"\n',
        encoding="utf-8",
    )
    agent_md = tmp_path / "monkeybot_config" / "AGENT.md"
    agent_md.write_text("custom persona\n", encoding="utf-8")
    mcp = tmp_path / "monkeybot_config" / "mcp.json"
    mcp.write_text('{"mcpServers": {"mine": {}}}\n', encoding="utf-8")
    yaml_path = tmp_path / "monkeybot_config" / "monkeybot.yaml"
    doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    doc.pop("memory", None)
    yaml_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    report = run_refresh(dest=tmp_path)
    text = allow.read_text(encoding="utf-8")
    assert "  - mempalace\n" in text
    assert '  - "officecli"\n' in text
    assert '  - "./custom-data/"\n' in text
    assert "  - ../memory/mempalace/\n" in text
    assert "my-custom-deny" in text
    assert agent_md.read_text(encoding="utf-8") == "custom persona\n"
    assert '"mine"' in mcp.read_text(encoding="utf-8")
    refreshed_yaml = yaml_path.read_text(encoding="utf-8")
    assert "engine: mempalace" in refreshed_yaml
    assert "custom persona" not in refreshed_yaml
    joined = "\n".join(report)
    assert "command_allowlist.yaml: updated" in joined
    assert "monkeybot.yaml: updated" in joined


def test_run_refresh_skips_custom_permissions_and_model(tmp_path: Path) -> None:
    run_new(dest=tmp_path, force=False, provider="nvidia", model="keep-me")
    perms = tmp_path / "monkeybot_config" / "permissions.yaml"
    perms.write_text("default: deny\nrules:\n  - tool: '*'\n    pattern: '*'\n    effect: deny\n", encoding="utf-8")
    yaml_path = tmp_path / "monkeybot_config" / "monkeybot.yaml"
    before = yaml_path.read_text(encoding="utf-8")

    report = run_refresh(dest=tmp_path)
    assert perms.read_text(encoding="utf-8").startswith("default: deny")
    after = yaml_path.read_text(encoding="utf-8")
    assert "keep-me" in after
    assert after == before or "keep-me" in after
    assert "permissions.yaml: skipped (customized)" in "\n".join(report)


def test_run_refresh_requires_existing_agent(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not a scaffolded agent"):
        run_refresh(dest=tmp_path)


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


def test_refresh_allowlist_does_not_emit_yaml_document_end() -> None:
    from monkeybot_cli.scaffold import _yaml_list_item

    item = _yaml_list_item("mempalace")
    assert "..." not in item
    assert item.strip() == '- "mempalace"'
