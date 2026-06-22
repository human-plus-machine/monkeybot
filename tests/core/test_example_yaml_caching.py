"""Tests that example YAML configs document ``model.enable_caching``."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_EXAMPLE_YAML = (
    REPO_ROOT / "src" / "monkeybot" / "templates" / "monkeybot.example.yaml"
)
CONFIG_EXAMPLE_YAML = REPO_ROOT / "monkeybot_config" / "monkeybot.example.yaml"


def _read_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _extract_enable_caching_block(text: str) -> str:
    lines = text.splitlines()
    start = next(
        i
        for i, line in enumerate(lines)
        if line.strip().startswith("# Prompt caching")
    )
    end = next(
        i
        for i, line in enumerate(lines[start:], start=start)
        if line.strip() == "enable_caching: true"
    )
    return "\n".join(lines[start : end + 1])


def test_templates_example_yaml_parses_and_has_enable_caching() -> None:
    data = _read_yaml(TEMPLATES_EXAMPLE_YAML)
    assert data["model"]["enable_caching"] is True


def test_config_example_yaml_parses_and_has_enable_caching() -> None:
    data = _read_yaml(CONFIG_EXAMPLE_YAML)
    assert data["model"]["enable_caching"] is True


def test_both_example_yamls_have_caching_comment() -> None:
    for path in (TEMPLATES_EXAMPLE_YAML, CONFIG_EXAMPLE_YAML):
        text = path.read_text(encoding="utf-8")
        assert "Default ON" in text
        assert "cache_control" in text


def test_both_example_yamls_caching_lines_identical() -> None:
    templates_text = TEMPLATES_EXAMPLE_YAML.read_text(encoding="utf-8")
    config_text = CONFIG_EXAMPLE_YAML.read_text(encoding="utf-8")
    assert _extract_enable_caching_block(templates_text) == _extract_enable_caching_block(
        config_text
    )
