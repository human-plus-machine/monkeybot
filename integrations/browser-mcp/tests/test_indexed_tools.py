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
    server._bh = None
    yield
    server._bh = original


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


def test_browser_input_by_index_uses_fill_input_not_manual_typing() -> None:
    helpers = MagicMock()
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing,
            "get_input_info",
            return_value={"selector": '[data-bmcp-idx="12"]', "tagName": "input"},
        ),
    ):
        result = json.loads(server.browser_input_by_index(12, "hello@example.com"))

    assert result == {"ok": True, "index": 12, "tagName": "input"}
    helpers.fill_input.assert_called_once_with(
        '[data-bmcp-idx="12"]', "hello@example.com", clear_first=True
    )
    helpers.press_key.assert_not_called()  # no hand-rolled clear/type loop


def test_browser_input_by_index_returns_error_on_stale_index() -> None:
    helpers = MagicMock()
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing,
            "get_input_info",
            side_effect=dom_indexing.ElementNotFoundError("Element index not found."),
        ),
    ):
        result = json.loads(server.browser_input_by_index(99, "text"))

    assert result == {"ok": False, "error": "Element index not found."}
    helpers.fill_input.assert_not_called()


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
