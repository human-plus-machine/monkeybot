"""Tests for the indexed-element MCP tools in browser_mcp.server.

Covers the error-handling contract: a stale/unknown index must return
{"ok": False, "error": ...} like the other domain-error tools (playbooks),
not propagate a raw exception through the MCP boundary.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from browser_mcp import dom_indexing, server


@pytest.fixture(autouse=True)
def _reset_bh_state():
    original = server._bh
    original_bound = server._bound_cdp
    server._bh = None
    server._bound_cdp = None
    dom_indexing.clear_registered_targets()
    yield
    server._bh = original
    server._bound_cdp = original_bound
    dom_indexing.clear_registered_targets()


def _patch_harness(helpers: MagicMock):
    return patch.object(server, "_browser_harness", return_value=(helpers, MagicMock()))


def test_browser_click_by_index_success() -> None:
    helpers = MagicMock()
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing, "get_rect", return_value={"x": 1, "y": 2, "tagName": "button"}
        ),
    ):
        result = json.loads(server.browser_click_by_index(35))

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
        result = json.loads(server.browser_click_by_index(35))

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
        result = json.loads(server.browser_input_by_index(12, "hello@example.com"))

    assert result == {
        "ok": True,
        "index": 12,
        "tagName": "input",
        "mode_used": "fast",
    }
    fill.assert_called_once_with(
        helpers, 12, "hello@example.com", clear_first=True, mode="auto"
    )
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
    fill.assert_called_once_with(
        helpers, 12, "hello", clear_first=True, mode="keys"
    )


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
        result = json.loads(server.browser_select_by_index(7, "Option A"))

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
        patch.object(server.playbooks, "list_playbook_names", return_value=[]),
        patch.object(dom_indexing, "settle", return_value={"quiet": True, "navigated": False}),
        patch.object(dom_indexing, "_register_driver_for_new_documents"),
    ):
        result = json.loads(server.browser_goto("https://example.test/next"))

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
        patch.object(server.playbooks, "list_playbook_names", return_value=[]),
        patch.object(dom_indexing, "settle", return_value={"quiet": True}),
        patch.object(dom_indexing, "_register_driver_for_new_documents"),
    ):
        server.browser_goto("https://example.test/")

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
        patch.object(server.playbooks, "list_playbook_names", return_value=[]),
        patch.object(dom_indexing, "settle", return_value={"quiet": True}),
        patch.object(dom_indexing, "_register_driver_for_new_documents"),
    ):
        server.browser_goto("https://example.test/other", new_tab=True)

    helpers.new_tab.assert_called_once_with("https://example.test/other")
    helpers.goto_url.assert_not_called()


def test_browser_switch_tab_registers_driver() -> None:
    helpers = MagicMock()
    helpers.switch_tab.return_value = "sid"
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "_register_driver_for_new_documents") as register,
    ):
        result = json.loads(server.browser_switch_tab("abc"))

    assert result == {"ok": True, "session_id": "sid"}
    register.assert_called_once_with(helpers)
