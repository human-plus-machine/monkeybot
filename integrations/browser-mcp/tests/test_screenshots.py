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


def test_env_paths_remain_stable_after_cwd_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "agent" / "workspace"
    shots = workspace / "browser" / "Screenshots"
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("BROWSER_MCP_SCREENSHOTS_DIR", str(shots))
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    assert screenshots.screenshots_dir() == shots.resolve()
    assert screenshots.workspace_root() == workspace.resolve()


def test_screenshot_retention_prunes_oldest_before_allocating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shots = tmp_path / "workspace" / "browser" / "Screenshots"
    shots.mkdir(parents=True)
    for index in range(3):
        path = shots / f"old-{index}.png"
        path.write_bytes(b"x")
        os.utime(path, (index + 1, index + 1))
    monkeypatch.setenv("BROWSER_MCP_SCREENSHOTS_DIR", str(shots))
    monkeypatch.setenv("BROWSER_MCP_SCREENSHOTS_MAX_FILES", "2")

    screenshots.allocate_screenshot_path()
    assert [path.name for path in sorted(shots.glob("old-*.png"))] == ["old-2.png"]


def test_zero_screenshot_limits_disable_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shots = tmp_path / "workspace" / "browser" / "Screenshots"
    shots.mkdir(parents=True)
    (shots / "old.png").write_bytes(b"x")
    monkeypatch.setenv("BROWSER_MCP_SCREENSHOTS_DIR", str(shots))
    monkeypatch.setenv("BROWSER_MCP_SCREENSHOTS_MAX_FILES", "0")
    monkeypatch.setenv("BROWSER_MCP_SCREENSHOTS_MAX_BYTES", "0")

    screenshots.allocate_screenshot_path()
    assert (shots / "old.png").is_file()
