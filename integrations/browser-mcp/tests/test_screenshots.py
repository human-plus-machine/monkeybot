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
    assert rel_path.endswith(".jpg")
    assert abs_path.parent.is_dir()


def test_allocate_screenshot_path_png_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace").mkdir()
    abs_path, rel_path = screenshots.allocate_screenshot_path("png")
    assert rel_path.endswith(".png")
    assert abs_path.suffix == ".png"


def test_jpeg_encode_is_at_least_70_percent_smaller_than_png(tmp_path: Path) -> None:
    from PIL import Image

    raw = os.urandom(1800 * 1200 * 3)
    img = Image.frombytes("RGB", (1800, 1200), raw)
    png_path = tmp_path / "src.png"
    img.save(png_path, format="PNG")
    png_bytes = png_path.stat().st_size
    dest = tmp_path / "out.jpg"
    screenshots.encode_screenshot(png_path, dest, fmt="jpeg", quality=60, max_dim=1800)
    jpeg_bytes = dest.read_bytes()
    assert jpeg_bytes[:2] == b"\xff\xd8"
    assert dest.stat().st_size <= png_bytes * 0.3


def test_draw_index_labels_paints_badges() -> None:
    from PIL import Image

    img = Image.new("RGB", (400, 300), (255, 255, 255))
    rects = {
        "1": {"x": 10, "y": 10, "width": 40, "height": 20},
        "2": {"x": 200, "y": 100, "width": 40, "height": 20},
    }
    out, labeled = screenshots.draw_index_labels(
        img, rects, css_width=400, css_height=300
    )
    assert labeled == 2
    assert out.getpixel((10, 10)) == screenshots.LABEL_FILL
    assert out.getpixel((200, 100)) == screenshots.LABEL_FILL
    assert out.getpixel((50, 200)) == (255, 255, 255)


def test_draw_index_labels_caps_at_150() -> None:
    from PIL import Image

    img = Image.new("RGB", (400, 300), (255, 255, 255))
    rects = {
        str(i): {"x": i % 20, "y": i % 15, "width": 4, "height": 4} for i in range(200)
    }
    _out, labeled = screenshots.draw_index_labels(
        img, rects, css_width=400, css_height=300
    )
    assert labeled == 150


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
