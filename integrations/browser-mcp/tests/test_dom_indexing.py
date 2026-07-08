"""Tests for browser_mcp.dom_indexing (indexed DOM interaction glue).

These test the Python-side wiring (asset loading, chunked injection, expression
building) against a fake `helpers.js`, not real DOM behavior -- the vendored JS
itself is exercised by hand against a live browser.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from browser_mcp import dom_indexing


def _fake_helpers(*, present: bool = False, tree: dict | None = None):
    """Simulate helpers.js for inject + getTree.

    First presence probe returns ``present``. After chunked inject, subsequent
    ``!!window.__bmcp`` probes return True. ``getTree`` returns ``tree``.
    """
    helpers = MagicMock()
    state = {"injected": present}

    def js(expression: str):
        if expression == "!!window.__bmcp":
            return state["injected"]
        if expression == "window.__bmcpChunks = []":
            return None
        if expression.startswith("window.__bmcpChunks.push("):
            return None
        if "atob(window.__bmcpChunks.join" in expression or "(0, eval)(src)" in expression:
            state["injected"] = True
            return None
        if expression.startswith("window.__bmcp.getTree("):
            return tree if tree is not None else {"tree": "", "elementCount": 0}
        if expression.startswith("window.__bmcp.getRect("):
            return {"x": 10, "y": 20}
        if expression.startswith("window.__bmcp.getInputInfo("):
            return {"selector": '[data-bmcp-idx="12"]', "tagName": "input"}
        if expression.startswith("window.__bmcp.selectOption("):
            return True
        raise AssertionError(f"unexpected js expression: {expression[:120]}")

    helpers.js.side_effect = js
    return helpers


def test_assets_load_and_are_nonempty() -> None:
    assert "__bmcpBuildDomTree" in dom_indexing._DOM_TREE_JS
    assert "__bmcp" in dom_indexing._DRIVER_JS
    assert len(dom_indexing._DOM_TREE_JS) > 1000
    assert len(dom_indexing._DRIVER_JS) > 500


def test_driver_source_exceeds_single_ipc_safe_chunk() -> None:
    """Regression: a single helpers.js(full_script) blows the 64 KiB IPC line limit."""
    assert len(dom_indexing._DRIVER_SOURCE) > dom_indexing._INJECT_CHUNK_CHARS


def test_b64_chunks_stay_under_limit() -> None:
    chunks = dom_indexing._b64_chunks(dom_indexing._DRIVER_SOURCE)
    assert len(chunks) >= 2
    assert all(len(c) <= dom_indexing._INJECT_CHUNK_CHARS for c in chunks)


def test_get_elements_skips_inject_when_driver_present() -> None:
    helpers = _fake_helpers(
        present=True, tree={"tree": "[0]<button>Go</button>", "elementCount": 1}
    )
    result = dom_indexing.get_elements(helpers, viewport_only=True)
    exprs = [c.args[0] for c in helpers.js.call_args_list]
    assert exprs[0] == "!!window.__bmcp"
    assert any(e.startswith("window.__bmcp.getTree(true)") for e in exprs)
    assert not any("__bmcpChunks" in e for e in exprs)
    assert result == {"tree": "[0]<button>Go</button>", "elementCount": 1}


def test_get_elements_chunk_injects_then_calls_getTree() -> None:
    helpers = _fake_helpers(
        present=False, tree={"tree": "[0]<button>Go</button>", "elementCount": 1}
    )
    result = dom_indexing.get_elements(helpers, viewport_only=True)
    exprs = [c.args[0] for c in helpers.js.call_args_list]
    assert exprs[0] == "!!window.__bmcp"
    assert "window.__bmcpChunks = []" in exprs
    assert any(e.startswith("window.__bmcpChunks.push(") for e in exprs)
    assert any("atob(window.__bmcpChunks.join" in e for e in exprs)
    assert any(e.startswith("window.__bmcp.getTree(true)") for e in exprs)
    # No single call should carry the full ~60KB source.
    assert all(len(e) < 40_000 for e in exprs)
    assert result == {"tree": "[0]<button>Go</button>", "elementCount": 1}


def test_get_elements_default_scans_full_page() -> None:
    helpers = _fake_helpers(present=True, tree={})
    dom_indexing.get_elements(helpers, viewport_only=False)
    exprs = [c.args[0] for c in helpers.js.call_args_list]
    assert any("window.__bmcp.getTree(false)" in e for e in exprs)


def test_get_rect_sends_short_followup_without_reinjecting_driver() -> None:
    helpers = MagicMock()
    helpers.js.return_value = {"x": 10, "y": 20}
    result = dom_indexing.get_rect(helpers, 35)
    (expression,), _ = helpers.js.call_args
    assert "__bmcpChunks" not in expression
    assert "window.__bmcp.getRect(35)" in expression
    assert result == {"x": 10, "y": 20}


def test_get_input_info_passes_index_without_reinjecting_driver() -> None:
    helpers = MagicMock()
    helpers.js.return_value = {"selector": '[data-bmcp-idx="12"]', "tagName": "input"}
    dom_indexing.get_input_info(helpers, 12)
    (expression,), _ = helpers.js.call_args
    assert "__bmcpChunks" not in expression
    assert "window.__bmcp.getInputInfo(12)" in expression


def test_select_option_escapes_and_passes_arguments() -> None:
    helpers = MagicMock()
    helpers.js.return_value = True
    result = dom_indexing.select_option(helpers, 7, 'Option "A"')
    (expression,), _ = helpers.js.call_args
    assert "window.__bmcp.selectOption(7" in expression
    assert '\\"A\\"' in expression  # json.dumps escapes embedded quotes
    assert result is True


def test_get_rect_raises_element_not_found_on_stale_index() -> None:
    helpers = MagicMock()
    helpers.js.side_effect = RuntimeError(
        "JavaScript evaluation failed: Error: No element at index 99. Call getTree() first (or the DOM changed)."
    )
    with pytest.raises(dom_indexing.ElementNotFoundError):
        dom_indexing.get_rect(helpers, 99)


def test_get_rect_reraises_unrelated_runtime_errors() -> None:
    helpers = MagicMock()
    helpers.js.side_effect = RuntimeError(
        "JavaScript evaluation failed: ReferenceError: foo is not defined"
    )
    with pytest.raises(RuntimeError) as exc_info:
        dom_indexing.get_rect(helpers, 1)
    assert not isinstance(exc_info.value, dom_indexing.ElementNotFoundError)
