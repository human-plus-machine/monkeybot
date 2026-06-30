"""Tests that packaged example YAML documents ``model.enable_caching``."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGED_EXAMPLE_YAML = (
    REPO_ROOT / "src" / "monkeybot" / "monkeybot_config" / "monkeybot.example.yaml"
)


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


def test_packaged_example_yaml_parses_and_has_enable_caching() -> None:
    data = _read_yaml(PACKAGED_EXAMPLE_YAML)
    assert data["model"]["enable_caching"] is True


def test_packaged_example_yaml_has_caching_comment() -> None:
    text = PACKAGED_EXAMPLE_YAML.read_text(encoding="utf-8")
    assert "Default ON" in text
    assert "cache_control" in text
    block = _extract_enable_caching_block(text)
    assert "enable_caching: true" in block
