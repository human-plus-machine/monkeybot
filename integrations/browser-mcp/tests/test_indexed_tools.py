"""Tests for the indexed-element MCP tools in browser_mcp.server.

Covers the error-handling contract: a stale/unknown index must return
{"ok": False, "error": ...} like the other domain-error tools (playbooks),
not propagate a raw exception through the MCP boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from browser_mcp import dom_indexing, server, tabs, backend, playbooks


@pytest.fixture(autouse=True)
def _reset_bh_state():
    original = backend._bh
    original_bound = backend._bound_cdp
    backend._bh = None
    backend._bound_cdp = None
    dom_indexing.clear_registered_targets()
    tabs.reset_registry()
    yield
    backend._bh = original
    backend._bound_cdp = original_bound
    dom_indexing.clear_registered_targets()
    tabs.reset_registry()


def _patch_harness(helpers: MagicMock):
    return patch.object(backend, "browser_harness", return_value=(helpers, MagicMock()))


def test_browser_click_by_index_success() -> None:
    helpers = MagicMock()
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing, "get_rect", return_value={"x": 1, "y": 2, "tagName": "button"}
        ),
    ):
        result = json.loads(server.browser_click_by_index(35, observe="none"))

    assert result == {"ok": True, "clicked": {"x": 1, "y": 2, "tagName": "button"}}
    helpers.click_at_xy.assert_called_once_with(1, 2)


def test_browser_click_by_index_returns_error_on_stale_index() -> None:
    helpers = MagicMock()
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing,
            "get_rect",
            side_effect=dom_indexing.ElementNotFoundError("Element index not found."),
        ),
    ):
        result = json.loads(server.browser_click_by_index(99))

    assert result == {"ok": False, "error": "Element index not found."}
    helpers.click_at_xy.assert_not_called()


def test_browser_click_by_index_warns_when_obscured() -> None:
    helpers = MagicMock()
    rect = {"x": 1, "y": 2, "tagName": "button", "obscuredBy": "div"}
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "get_rect", return_value=rect),
    ):
        result = json.loads(server.browser_click_by_index(35, observe="none"))

    assert result["ok"] is True
    assert result["clicked"] == rect
    assert result["warning"] == "target obscured by div"
    helpers.click_at_xy.assert_called_once_with(1, 2)


def test_browser_input_by_index_uses_fill() -> None:
    helpers = MagicMock()
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing,
            "fill",
            return_value={"ok": True, "tagName": "input", "mode_used": "fast"},
        ) as fill,
    ):
        result = json.loads(server.browser_input_by_index(12, "hello@example.com", observe="none"))

    assert result == {
        "ok": True,
        "index": 12,
        "tagName": "input",
        "mode_used": "fast",
    }
    fill.assert_called_once()
    args, kwargs = fill.call_args
    assert args[1:] == (12, "hello@example.com")
    assert kwargs == {"clear_first": True, "mode": "auto"}
    helpers.press_key.assert_not_called()


def test_browser_input_by_index_env_default_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROWSER_MCP_FILL_MODE", "keys")
    helpers = MagicMock()
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing,
            "fill",
            return_value={"ok": True, "tagName": "input", "mode_used": "keys"},
        ) as fill,
    ):
        json.loads(server.browser_input_by_index(12, "hello"))
    fill.assert_called_once()
    args, kwargs = fill.call_args
    assert args[1:] == (12, "hello")
    assert kwargs == {"clear_first": True, "mode": "keys"}


def test_browser_input_by_index_returns_error_on_stale_index() -> None:
    helpers = MagicMock()
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing,
            "fill",
            side_effect=dom_indexing.ElementNotFoundError("Element index not found."),
        ),
    ):
        result = json.loads(server.browser_input_by_index(99, "text"))

    assert result == {"ok": False, "error": "Element index not found."}


def test_browser_select_by_index_success() -> None:
    helpers = MagicMock()
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "select_option", return_value=True),
    ):
        result = json.loads(server.browser_select_by_index(7, "Option A", observe="none"))

    assert result == {"ok": True, "index": 7, "selected": "Option A"}


def test_browser_select_by_index_returns_error_on_stale_index() -> None:
    helpers = MagicMock()
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing,
            "select_option",
            side_effect=dom_indexing.ElementNotFoundError("Element index not found."),
        ),
    ):
        result = json.loads(server.browser_select_by_index(99, "Option A"))

    assert result == {"ok": False, "error": "Element index not found."}


def test_browser_goto_reuses_real_tab() -> None:
    helpers = MagicMock(spec=["current_tab", "goto_url", "new_tab", "js", "page_info"])
    helpers.current_tab.return_value = {
        "targetId": "t1",
        "url": "https://example.test/page",
    }
    helpers.page_info.return_value = {"url": "https://example.test/next", "title": "t"}
    helpers.js.return_value = True
    with (
        _patch_harness(helpers),
        patch.object(playbooks, "list_playbook_names", return_value=[]),
        patch.object(dom_indexing, "settle", return_value={"quiet": True, "navigated": False}),
        patch.object(dom_indexing, "_register_driver_for_new_documents"),
    ):
        result = json.loads(server.browser_goto("https://example.test/next", observe="none"))

    helpers.goto_url.assert_called_once_with("https://example.test/next")
    helpers.new_tab.assert_not_called()
    assert result["url"] == "https://example.test/next"
    assert result["playbooks"] == []


def test_browser_goto_opens_tab_when_blank() -> None:
    helpers = MagicMock(spec=["current_tab", "goto_url", "new_tab", "js", "page_info"])
    helpers.current_tab.return_value = {"targetId": "t1", "url": "about:blank"}
    helpers.page_info.return_value = {"url": "https://example.test/", "title": "t"}
    helpers.js.return_value = True
    with (
        _patch_harness(helpers),
        patch.object(playbooks, "list_playbook_names", return_value=[]),
        patch.object(dom_indexing, "settle", return_value={"quiet": True}),
        patch.object(dom_indexing, "_register_driver_for_new_documents"),
    ):
        server.browser_goto("https://example.test/", observe="none")

    helpers.new_tab.assert_called_once_with("https://example.test/")
    helpers.goto_url.assert_not_called()


def test_browser_goto_new_tab_flag_opens_tab() -> None:
    helpers = MagicMock(spec=["current_tab", "goto_url", "new_tab", "js", "page_info"])
    helpers.current_tab.return_value = {
        "targetId": "t1",
        "url": "https://example.test/page",
    }
    helpers.page_info.return_value = {"url": "https://example.test/other", "title": "t"}
    helpers.js.return_value = True
    with (
        _patch_harness(helpers),
        patch.object(playbooks, "list_playbook_names", return_value=[]),
        patch.object(dom_indexing, "settle", return_value={"quiet": True}),
        patch.object(dom_indexing, "_register_driver_for_new_documents"),
    ):
        server.browser_goto("https://example.test/other", new_tab=True, observe="none")

    helpers.new_tab.assert_called_once_with("https://example.test/other", background=False)
    helpers.goto_url.assert_not_called()


def test_browser_switch_tab_registers_driver() -> None:
    helpers = MagicMock()
    helpers.switch_tab.return_value = "sid"
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "_register_driver_for_new_documents") as register,
    ):
        result = json.loads(server.browser_switch_tab("abc", observe="none"))

    assert result == {"ok": True, "session_id": "sid"}
    register.assert_called_once_with(helpers)


def _write_png(path: str | None = None, full: bool = False, max_dim: int | None = None) -> str:
    from PIL import Image

    dest = str(path)
    Image.new("RGB", (40, 30), (10, 20, 30)).save(dest, "PNG")
    return dest


def test_browser_screenshot_defaults_to_jpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    helpers = MagicMock(spec=["capture_screenshot", "page_info", "js"])
    helpers.capture_screenshot.side_effect = _write_png
    helpers.page_info.return_value = {
        "url": "https://example.test/",
        "title": "t",
        "w": 40,
        "h": 30,
    }
    with _patch_harness(helpers):
        result = json.loads(server.browser_screenshot())

    assert result["ok"] is True
    assert result["format"] == "jpeg"
    assert result["path"].endswith(".jpg")
    assert isinstance(result["bytes"], int) and result["bytes"] > 0
    helpers.capture_screenshot.assert_called_once()
    assert helpers.capture_screenshot.call_args.kwargs["max_dim"] is None
    saved = tmp_path / "workspace" / result["path"].removeprefix("./")
    assert saved.read_bytes()[:2] == b"\xff\xd8"


def test_browser_screenshot_png_keeps_png_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    helpers = MagicMock(spec=["capture_screenshot", "page_info", "js"])
    helpers.capture_screenshot.side_effect = _write_png
    helpers.page_info.return_value = {
        "url": "https://example.test/",
        "title": "t",
        "w": 40,
        "h": 30,
    }
    with _patch_harness(helpers):
        result = json.loads(server.browser_screenshot(format="png"))

    assert result["ok"] is True
    assert result["format"] == "png"
    assert result["path"].endswith(".png")


def test_browser_screenshot_annotate_uses_get_rects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    helpers = MagicMock(spec=["capture_screenshot", "page_info", "js"])
    helpers.capture_screenshot.side_effect = _write_png
    helpers.page_info.return_value = {
        "url": "https://example.test/",
        "title": "t",
        "w": 40,
        "h": 30,
    }
    helpers.js.return_value = 2
    rects = {
        "rects": {"1": {"x": 0, "y": 0, "width": 10, "height": 10}},
        "cssWidth": 40,
        "cssHeight": 30,
        "dpr": 1,
    }
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "get_rects", return_value=rects) as get_rects,
        patch.object(dom_indexing, "get_elements") as get_elements,
    ):
        result = json.loads(server.browser_screenshot(annotate=True))

    assert result["ok"] is True
    assert result["annotated"] is True
    assert result["labeled"] == 1
    get_elements.assert_not_called()
    get_rects.assert_called_once()
    assert get_rects.call_args.kwargs["scroll"] is False
    js_exprs = [str(c.args[0]) for c in helpers.js.call_args_list if c.args]
    assert not any("getRect(" in expr and "getRects" not in expr for expr in js_exprs)
