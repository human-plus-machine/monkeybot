"""Tests for the CLI gateway manager."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.cli.gateway_manager import _find_workspace_dir, _url_is_local


def test_find_workspace_dir(tmp_path: Path) -> None:
    config_dir = tmp_path / "monkeybot_config"
    config_dir.mkdir()
    (config_dir / "monkeybot.yaml").write_text("harness:\n  mode: realtime\n")

    found = _find_workspace_dir(tmp_path)
    assert found == tmp_path


def test_find_workspace_dir_from_child(tmp_path: Path) -> None:
    config_dir = tmp_path / "monkeybot_config"
    config_dir.mkdir()
    (config_dir / "monkeybot.yaml").write_text("harness:\n  mode: realtime\n")

    child = tmp_path / "workspace" / "data"
    child.mkdir(parents=True)
    found = _find_workspace_dir(child)
    assert found == tmp_path


def test_find_workspace_dir_missing(tmp_path: Path) -> None:
    assert _find_workspace_dir(tmp_path) is None


@pytest.mark.parametrize(
    "url,expected",
    [
        ("ws://localhost:8787", True),
        ("ws://127.0.0.1:8787", True),
        ("wss://localhost:8787", True),
        ("ws://example.com:8787", False),
        ("wss://api.example.com/realtime", False),
    ],
)
def test_url_is_local(url: str, expected: bool) -> None:
    assert _url_is_local(url) is expected
