"""Tests for browser_mcp.dom_indexing (indexed DOM interaction glue).

These test the Python-side wiring (asset loading, chunked injection, expression
building) against a fake `helpers.js`, not real DOM behavior -- the vendored JS
itself is exercised by hand against a live browser.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from browser_mcp import dom_indexing


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    dom_indexing.clear_registered_targets()
    yield
    dom_indexing.clear_registered_targets()


def _fake_helpers(*, present: bool = False, tree: dict | None = None):
    """Simulate helpers.js for inject + getTree.

    First presence probe returns ``present``. After chunked inject, subsequent
    ``!!window.__bmcp`` probes return True. ``getTree`` returns ``tree``.
    No ``cdp`` / ``add_init_script`` so hasattr checks take the chunked path.
    """
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
        if expression.startswith("window.__bmcp.getRects("):
            return {
                "rects": {"1": {"x": 0, "y": 0, "width": 10, "height": 10}},
                "cssWidth": 800,
                "cssHeight": 600,
                "dpr": 1,
            }
        if expression.startswith("window.__bmcp.getInputInfo("):
            return {"selector": '[data-bmcp-idx="12"]', "tagName": "input"}
        if expression.startswith("window.__bmcp.selectOption("):
            return True
        if expression.startswith("window.__bmcp.fill("):
            return {"ok": True, "value": "hello", "tagName": "input", "needsKeys": False}
        if expression.startswith("window.__bmcp.settle("):
            return {"quiet": True, "mutations": 0}
        if expression == "window.__bmcpInjectError || null":
            return None
        raise AssertionError(f"unexpected js expression: {expression[:120]}")

    helpers = MagicMock(spec=["js"])
    helpers.js.side_effect = js
    return helpers


def _cdp_helpers(*, target_id: str = "t1"):
    calls: list[tuple[str, str]] = []

    def cdp(method: str, **params: str) -> dict:
        calls.append((method, params.get("source", "")))
        return {}

    def js(expression: str):
        if expression == "window.__bmcpChunks = []":
            return None
        if expression.startswith("window.__bmcpChunks.push("):
            return None
        if "atob(window.__bmcpChunks.join" in expression or "(0, eval)(src)" in expression:
            return None
        if expression.startswith("window.__bmcp.getTree("):
            return {"tree": "[0]<a>x</a>", "elementCount": 1}
        if expression.startswith("window.__bmcp.fill("):
            return {"ok": True, "value": "hello", "tagName": "input", "needsKeys": False}
        if expression.startswith("window.__bmcp.settle("):
            return {"quiet": True, "mutations": 0}
        if expression == "window.__bmcpInjectError || null":
            return None
        raise AssertionError(f"unexpected js expression: {expression[:120]}")

    helpers = SimpleNamespace(
        cdp=MagicMock(side_effect=cdp),
        js=MagicMock(side_effect=js),
        current_tab=MagicMock(return_value={"targetId": target_id, "url": "http://example.test/"}),
        fill_input=MagicMock(),
    )
    helpers._cdp_calls = calls
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
    helpers = MagicMock(spec=["js"])
    helpers.js.return_value = {"x": 10, "y": 20}
    result = dom_indexing.get_rect(helpers, 35)
    (expression,), _ = helpers.js.call_args
    assert "__bmcpChunks" not in expression
    assert "window.__bmcp.getRect(35)" in expression
    assert result == {"x": 10, "y": 20}


def test_get_rects_defaults_to_no_scroll() -> None:
    helpers = MagicMock(spec=["js"])
    payload = {
        "rects": {"1": {"x": 4, "y": 8, "width": 10, "height": 12}},
        "cssWidth": 800,
        "cssHeight": 600,
        "dpr": 2,
    }
    helpers.js.return_value = payload
    result = dom_indexing.get_rects(helpers)
    (expression,), _ = helpers.js.call_args
    assert "__bmcpChunks" not in expression
    assert "window.__bmcp.getRects(null, " in expression
    assert '"scroll": false' in expression
    assert '"full": false' in expression
    assert result == payload


def test_get_rects_passes_indices_scroll_and_full() -> None:
    helpers = MagicMock(spec=["js"])
    helpers.js.return_value = {
        "rects": {},
        "cssWidth": 100,
        "cssHeight": 200,
        "dpr": 1,
    }
    dom_indexing.get_rects(helpers, [3, 5], scroll=True, full=True)
    (expression,), _ = helpers.js.call_args
    assert "window.__bmcp.getRects([3, 5], " in expression
    assert '"scroll": true' in expression
    assert '"full": true' in expression


def test_get_rects_normalizes_unexpected_payload() -> None:
    helpers = MagicMock(spec=["js"])
    helpers.js.return_value = {"1": {"x": 0, "y": 0}}
    result = dom_indexing.get_rects(helpers)
    assert result == {"rects": {}, "cssWidth": 0, "cssHeight": 0, "dpr": 1}


def test_get_input_info_passes_index_without_reinjecting_driver() -> None:
    helpers = MagicMock(spec=["js"])
    helpers.js.return_value = {"selector": '[data-bmcp-idx="12"]', "tagName": "input"}
    dom_indexing.get_input_info(helpers, 12)
    (expression,), _ = helpers.js.call_args
    assert "__bmcpChunks" not in expression
    assert "window.__bmcp.getInputInfo(12)" in expression


def test_select_option_escapes_and_passes_arguments() -> None:
    helpers = MagicMock(spec=["js"])
    helpers.js.return_value = True
    result = dom_indexing.select_option(helpers, 7, 'Option "A"')
    (expression,), _ = helpers.js.call_args
    assert "window.__bmcp.selectOption(7" in expression
    assert '\\"A\\"' in expression  # json.dumps escapes embedded quotes
    assert result is True


def test_get_rect_raises_element_not_found_on_stale_index() -> None:
    helpers = MagicMock(spec=["js"])
    helpers.js.side_effect = RuntimeError(
        "JavaScript evaluation failed: Error: No element at index 99. Call getTree() first (or the DOM changed)."
    )
    with pytest.raises(dom_indexing.ElementNotFoundError):
        dom_indexing.get_rect(helpers, 99)


def test_get_rect_reraises_unrelated_runtime_errors() -> None:
    helpers = MagicMock(spec=["js"])
    helpers.js.side_effect = RuntimeError(
        "JavaScript evaluation failed: ReferenceError: foo is not defined"
    )
    with pytest.raises(RuntimeError) as exc_info:
        dom_indexing.get_rect(helpers, 1)
    assert not isinstance(exc_info.value, dom_indexing.ElementNotFoundError)


def test_register_driver_issues_chunk_plus_join_cdp_calls() -> None:
    helpers = _cdp_helpers()
    dom_indexing._register_driver_for_new_documents(helpers)
    methods = [m for m, _ in helpers._cdp_calls]
    chunks = dom_indexing._b64_chunks(dom_indexing._DRIVER_SOURCE)
    assert methods == ["Page.addScriptToEvaluateOnNewDocument"] * (len(chunks) + 1)
    for _, source in helpers._cdp_calls:
        assert len(source) < 60_000
        assert "if (window.__bmcp) return;" in source
        assert source.strip().startswith("(function(){")
    assert helpers.js.call_count >= 2  # current-document inject


def test_register_driver_is_once_per_target() -> None:
    helpers = _cdp_helpers()
    dom_indexing._register_driver_for_new_documents(helpers)
    first = helpers.cdp.call_count
    assert first == len(dom_indexing._b64_chunks(dom_indexing._DRIVER_SOURCE)) + 1
    dom_indexing._register_driver_for_new_documents(helpers)
    assert helpers.cdp.call_count == first


def test_register_driver_again_after_clear() -> None:
    helpers = _cdp_helpers()
    dom_indexing._register_driver_for_new_documents(helpers)
    first = helpers.cdp.call_count
    dom_indexing.clear_registered_targets()
    dom_indexing._register_driver_for_new_documents(helpers)
    assert helpers.cdp.call_count == first * 2


def test_get_elements_after_registration_is_one_js_call() -> None:
    helpers = _cdp_helpers()
    dom_indexing._register_driver_for_new_documents(helpers)
    helpers.js.reset_mock()
    result = dom_indexing.get_elements(helpers, viewport_only=True)
    assert helpers.js.call_count == 1
    assert helpers.js.call_args.args[0] == "window.__bmcp.getTree(true)"
    assert result["elementCount"] == 1


def test_fill_auto_uses_fast_when_value_matches() -> None:
    helpers = _fake_helpers(present=True)
    result = dom_indexing.fill(helpers, 3, "hello", clear_first=True, mode="auto")
    assert result["mode_used"] == "fast"
    assert result["tagName"] == "input"


def test_fill_auto_falls_back_on_value_mismatch() -> None:
    helpers = MagicMock(spec=["js", "fill_input"])

    def js(expression: str):
        if expression == "!!window.__bmcp":
            return True
        if expression.startswith("window.__bmcp.fill("):
            return {"ok": True, "value": "old", "tagName": "input", "needsKeys": False}
        if expression.startswith("window.__bmcp.getInputInfo("):
            return {"selector": '[data-bmcp-idx="3"]', "tagName": "input"}
        raise AssertionError(expression[:120])

    helpers.js.side_effect = js
    result = dom_indexing.fill(helpers, 3, "hello", mode="auto")
    helpers.fill_input.assert_called_once_with('[data-bmcp-idx="3"]', "hello", clear_first=True)
    assert result["mode_used"] == "keys"


def test_fill_auto_falls_back_on_needs_keys() -> None:
    helpers = MagicMock(spec=["js", "fill_input"])

    def js(expression: str):
        if expression == "!!window.__bmcp":
            return True
        if expression.startswith("window.__bmcp.fill("):
            return {"ok": True, "value": "hello", "tagName": "input", "needsKeys": True}
        if expression.startswith("window.__bmcp.getInputInfo("):
            return {"selector": '[data-bmcp-idx="3"]', "tagName": "input"}
        raise AssertionError(expression[:120])

    helpers.js.side_effect = js
    result = dom_indexing.fill(helpers, 3, "hello", mode="auto")
    helpers.fill_input.assert_called_once()
    assert result["mode_used"] == "keys"


def test_fill_keys_skips_in_page_fill() -> None:
    helpers = MagicMock(spec=["js", "fill_input"])

    def js(expression: str):
        if expression == "!!window.__bmcp":
            return True
        if expression.startswith("window.__bmcp.fill("):
            raise AssertionError("mode=keys must not call __bmcp.fill")
        if expression.startswith("window.__bmcp.getInputInfo("):
            return {"selector": '[data-bmcp-idx="3"]', "tagName": "input"}
        raise AssertionError(expression[:120])

    helpers.js.side_effect = js
    result = dom_indexing.fill(helpers, 3, "hello", mode="keys")
    helpers.fill_input.assert_called_once()
    assert result["mode_used"] == "keys"


def test_fill_fast_never_falls_back() -> None:
    helpers = MagicMock(spec=["js", "fill_input"])

    def js(expression: str):
        if expression == "!!window.__bmcp":
            return True
        if expression.startswith("window.__bmcp.fill("):
            return {"ok": True, "value": "old", "tagName": "input", "needsKeys": False}
        raise AssertionError(expression[:120])

    helpers.js.side_effect = js
    result = dom_indexing.fill(helpers, 3, "hello", mode="fast")
    helpers.fill_input.assert_not_called()
    assert result["mode_used"] == "fast"
    assert result["value"] == "old"


def test_settle_returns_navigated_on_context_destroyed() -> None:
    helpers = MagicMock(spec=["js"])

    def js(expression: str):
        if expression == "!!window.__bmcp":
            return True
        if expression.startswith("window.__bmcp.settle("):
            raise RuntimeError("Execution context was destroyed")
        raise AssertionError(expression[:120])

    helpers.js.side_effect = js
    assert dom_indexing.settle(helpers) == {"quiet": True, "navigated": True}


def test_get_elements_reports_inject_error() -> None:
    helpers = MagicMock(spec=["js"])

    def js(expression: str):
        if expression == "!!window.__bmcp":
            return False
        if expression == "window.__bmcpChunks = []":
            return None
        if expression.startswith("window.__bmcpChunks.push("):
            return None
        if "atob(window.__bmcpChunks.join" in expression:
            return None
        if expression.startswith("window.__bmcp.getTree("):
            raise RuntimeError("window.__bmcp is not defined")
        if expression == "window.__bmcpInjectError || null":
            return "EvalError: CSP"
        raise AssertionError(expression[:120])

    helpers.js.side_effect = js
    result = dom_indexing.get_elements(helpers, viewport_only=True)
    assert result["error"] == "EvalError: CSP"
    assert result["elementCount"] == 0
