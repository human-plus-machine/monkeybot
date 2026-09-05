"""Unit tests for event-driven browser_wait_for / browser_wait_idle."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from browser_mcp import dom_indexing, server, tabs, backend


def setup_function() -> None:
    backend._bh = None
    backend._bound_cdp = None
    dom_indexing.clear_registered_targets()
    tabs.reset_registry()


def teardown_function() -> None:
    backend._bh = None
    backend._bound_cdp = None
    dom_indexing.clear_registered_targets()
    tabs.reset_registry()


def _patch_harness(helpers: MagicMock):
    return patch.object(backend, "browser_harness", return_value=(helpers, MagicMock()))


def test_browser_wait_for_uses_one_js_call_not_wait_for_element() -> None:
    helpers = MagicMock(spec=["js"])
    helpers.js.return_value = {"found": True}
    with _patch_harness(helpers):
        result = json.loads(server.browser_wait_for("#page-2"))

    assert result == {"ok": True, "found": True}
    helpers.js.assert_called_once()
    expr = helpers.js.call_args.args[0]
    assert "MutationObserver" in expr
    assert "querySelector" in expr
    assert "#page-2" in expr
    assert not hasattr(helpers, "wait_for_element")


def test_browser_wait_for_visible_uses_check_visibility() -> None:
    helpers = MagicMock(spec=["js"])
    helpers.js.return_value = {"found": True}
    with _patch_harness(helpers):
        json.loads(server.browser_wait_for("#x", visible=True))

    expr = helpers.js.call_args.args[0]
    assert "checkVisibility" in expr


def test_browser_wait_for_chunks_long_timeout() -> None:
    helpers = MagicMock(spec=["js"])
    helpers.js.return_value = {"found": False}
    with _patch_harness(helpers):
        result = json.loads(server.browser_wait_for("#missing", timeout=10.0))

    assert result == {"ok": False, "found": False}
    assert helpers.js.call_count == 3
    assert not hasattr(helpers, "wait_for_element")


def test_browser_wait_for_invalid_selector() -> None:
    helpers = MagicMock(spec=["js"])
    helpers.js.return_value = {"found": False, "error": "invalid selector"}
    with _patch_harness(helpers):
        result = json.loads(server.browser_wait_for("["))

    assert result == {"ok": False, "found": False}
    helpers.js.assert_called_once()


def test_browser_wait_idle_settles_after_network() -> None:
    helpers = MagicMock(spec=["wait_for_network_idle"])
    helpers.wait_for_network_idle.return_value = True
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing, "settle", return_value={"quiet": True, "navigated": False}
        ) as settle,
    ):
        result = json.loads(server.browser_wait_idle(timeout=2.0, idle_ms=200))

    helpers.wait_for_network_idle.assert_called_once_with(timeout=2.0, idle_ms=200)
    settle.assert_called_once()
    assert result == {
        "ok": True,
        "idle": True,
        "quiet": True,
        "navigated": False,
    }


def test_browser_wait_idle_skips_settle_on_network_timeout() -> None:
    helpers = MagicMock(spec=["wait_for_network_idle"])
    helpers.wait_for_network_idle.return_value = False
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "settle") as settle,
    ):
        result = json.loads(server.browser_wait_idle())

    settle.assert_not_called()
    assert result == {
        "ok": False,
        "idle": False,
        "quiet": False,
        "navigated": False,
    }


def test_wait_for_selector_navigation_returns_not_found() -> None:
    helpers = MagicMock(spec=["js"])
    helpers.js.side_effect = RuntimeError("Execution context was destroyed")
    result = dom_indexing.wait_for_selector(helpers, "#x", timeout=1.0)
    assert result == {"found": False, "navigated": True}
