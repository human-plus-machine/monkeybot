"""Tests for browser_mcp.dom_indexing (indexed DOM interaction glue).

These test the Python-side wiring (asset loading, injection-guard construction,
expression building) against a fake `helpers.js`, not real DOM behavior --
the vendored JS itself is exercised by hand against a live browser.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from browser_mcp import dom_indexing


def _fake_helpers(return_value=None):
    helpers = MagicMock()
    helpers.js.return_value = return_value
    return helpers


def test_assets_load_and_are_nonempty() -> None:
    assert "__bmcpBuildDomTree" in dom_indexing._DOM_TREE_JS
    assert "__bmcp" in dom_indexing._DRIVER_JS
    assert len(dom_indexing._DOM_TREE_JS) > 1000
    assert len(dom_indexing._DRIVER_JS) > 500


def test_inject_guard_wraps_both_scripts_idempotently() -> None:
    guard = dom_indexing._INJECT_GUARD
    assert guard.startswith("if (!window.__bmcp) {")
    assert guard.rstrip().endswith("}")
    assert "__bmcpBuildDomTree" in guard
    assert "window.__bmcp = " in guard


def test_get_elements_injects_driver_and_calls_getTree() -> None:
    """get_elements is the sole injection point (only call site that sends _INJECT_GUARD)."""
    helpers = _fake_helpers({"tree": "[0]<button>Go</button>", "elementCount": 1})
    result = dom_indexing.get_elements(helpers, viewport_only=True)

    helpers.js.assert_called_once()
    (expression,), _ = helpers.js.call_args
    assert dom_indexing._INJECT_GUARD in expression
    assert "window.__bmcp.getTree(true)" in expression
    assert result == {"tree": "[0]<button>Go</button>", "elementCount": 1}


def test_get_elements_default_scans_full_page() -> None:
    helpers = _fake_helpers({})
    dom_indexing.get_elements(helpers, viewport_only=False)
    (expression,), _ = helpers.js.call_args
    assert "window.__bmcp.getTree(false)" in expression


def test_get_rect_sends_short_followup_without_reinjecting_driver() -> None:
    helpers = _fake_helpers({"x": 10, "y": 20})
    result = dom_indexing.get_rect(helpers, 35)
    (expression,), _ = helpers.js.call_args
    assert dom_indexing._INJECT_GUARD not in expression
    assert "window.__bmcp.getRect(35)" in expression
    assert result == {"x": 10, "y": 20}


def test_get_input_info_passes_index_without_reinjecting_driver() -> None:
    helpers = _fake_helpers({"selector": '[data-bmcp-idx="12"]', "tagName": "input"})
    dom_indexing.get_input_info(helpers, 12)
    (expression,), _ = helpers.js.call_args
    assert dom_indexing._INJECT_GUARD not in expression
    assert "window.__bmcp.getInputInfo(12)" in expression


def test_select_option_escapes_and_passes_arguments() -> None:
    helpers = _fake_helpers(True)
    result = dom_indexing.select_option(helpers, 7, 'Option "A"')
    (expression,), _ = helpers.js.call_args
    assert "window.__bmcp.selectOption(7" in expression
    assert '\\"A\\"' in expression  # json.dumps escapes embedded quotes
    assert result is True


def test_get_rect_raises_element_not_found_on_stale_index() -> None:
    helpers = _fake_helpers()
    helpers.js.side_effect = RuntimeError(
        "JavaScript evaluation failed: Error: No element at index 99. Call getTree() first (or the DOM changed)."
    )
    with pytest.raises(dom_indexing.ElementNotFoundError):
        dom_indexing.get_rect(helpers, 99)


def test_get_rect_reraises_unrelated_runtime_errors() -> None:
    helpers = _fake_helpers()
    helpers.js.side_effect = RuntimeError("JavaScript evaluation failed: ReferenceError: foo is not defined")
    with pytest.raises(RuntimeError) as exc_info:
        dom_indexing.get_rect(helpers, 1)
    assert not isinstance(exc_info.value, dom_indexing.ElementNotFoundError)
