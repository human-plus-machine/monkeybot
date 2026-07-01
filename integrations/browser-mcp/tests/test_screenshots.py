"""Tests for workspace screenshot path resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from browser_mcp import screenshots


@pytest.fixture(autouse=True)
def _clear_screenshot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROWSER_MCP_SCREENSHOTS_DIR", raising=False)
    monkeypatch.delenv("MONKEYBOT_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)


def test_screenshots_dir_defaults_under_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace").mkdir()
    assert screenshots.screenshots_dir() == (tmp_path / "workspace" / "browser" / "Screenshots").resolve()


def test_screenshots_dir_from_env_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BROWSER_MCP_SCREENSHOTS_DIR", "./workspace/browser/Screenshots")
    assert screenshots.screenshots_dir() == (tmp_path / "workspace" / "browser" / "Screenshots").resolve()


def test_workspace_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / "workspace"
    shots = ws / "browser" / "Screenshots"
    shots.mkdir(parents=True)
    png = shots / "shot-test.png"
    png.write_bytes(b"\x89PNG\r\n")
    assert screenshots.workspace_relative(png) == "./browser/Screenshots/shot-test.png"


def test_allocate_screenshot_path_creates_unique_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace").mkdir()
    abs_path, rel_path = screenshots.allocate_screenshot_path()
    assert abs_path.parent == screenshots.screenshots_dir()
    assert rel_path.startswith("./browser/Screenshots/shot-")
    assert rel_path.endswith(".png")
    assert abs_path.parent.is_dir()
