"""Unit tests for Phase 4 act-then-observe."""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from browser_mcp import dom_indexing, server, tabs, backend, playbooks


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_MCP_QUIET_MS", "1")
    monkeypatch.setenv("BROWSER_MCP_SETTLE_MS", "200")
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


def _tab(tid: str, url: str, title: str = "t") -> dict:
    return {"targetId": tid, "target_id": tid, "url": url, "title": title}


def _helpers(*, url: str = "https://a.test/") -> MagicMock:
    helpers = MagicMock()
    row = _tab("aaa", url, "A")
    helpers.list_tabs.return_value = [row]
    helpers.current_tab.return_value = dict(row)
    helpers.page_info.return_value = {"url": url, "title": "A", "w": 800, "h": 600}
    helpers.js.return_value = True
    helpers.switch_tab.return_value = "sid"
    return helpers


def _tree(url: str, text: str, **extra: object) -> dict:
    payload: dict = {
        "tree": text,
        "elementCount": len([ln for ln in text.splitlines() if ln.strip()]),
        "url": url,
        "title": "A",
        "truncated": False,
        "below_viewport": 0,
        "omitted": 0,
    }
    payload.update(extra)
    return payload


@contextmanager
def _action_ctx(helpers: MagicMock, trees: list[dict], *, navigated: bool = False):
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing, "settle", return_value={"quiet": True, "navigated": navigated}
        ),
        patch.object(dom_indexing, "get_elements", side_effect=trees),
        patch.object(dom_indexing, "_register_driver_for_new_documents"),
        patch.object(
            dom_indexing,
            "get_rect",
            return_value={"x": 10, "y": 20, "tagName": "button"},
        ),
        patch.object(
            dom_indexing,
            "fill",
            return_value={"ok": True, "tagName": "input", "mode_used": "fast"},
        ),
        patch.object(dom_indexing, "select_option", return_value=True),
        patch.object(playbooks, "list_playbook_names", return_value=[]),
    ):
        yield


def test_click_observe_diff_includes_observation() -> None:
    helpers = _helpers()
    trees = [
        _tree("https://a.test/", "[0]<button>A />\n[1]<button>B />"),
        _tree("https://a.test/", "[0]<button>A />\n[2]<button>C />"),
    ]
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "settle", return_value={"quiet": True, "navigated": False}) as settle,
        patch.object(dom_indexing, "get_elements", side_effect=trees),
        patch.object(dom_indexing, "_register_driver_for_new_documents"),
        patch.object(
            dom_indexing,
            "get_rect",
            return_value={"x": 10, "y": 20, "tagName": "button"},
        ),
    ):
        json.loads(server.browser_tabs())
        json.loads(server.browser_get_elements())
        result = json.loads(server.browser_click_by_index(0, observe="diff"))
    assert result["ok"] is True
    assert result["clicked"]["x"] == 10
    assert result["action"]["type"] == "click"
    assert result["action"]["index"] == 0
    assert result["page"]["navigated"] is False
    assert result["observation"]["mode"] == "diff"
    assert result["observation"]["added"] == ["[2]<button>C />"]
    assert result["observation"]["removed"] == ["[1]<button>B />"]
    assert settle.call_count == 1


def test_observe_none_is_legacy_shape() -> None:
    helpers = _helpers()
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "get_rect", return_value={"x": 1, "y": 2}),
        patch.object(dom_indexing, "settle") as settle,
        patch.object(dom_indexing, "get_elements") as get_elements,
    ):
        json.loads(server.browser_tabs())
        result = json.loads(server.browser_click_by_index(3, observe="none"))
    assert result == {"ok": True, "clicked": {"x": 1, "y": 2}}
    settle.assert_not_called()
    get_elements.assert_not_called()


def test_observe_full_returns_tree() -> None:
    helpers = _helpers()
    trees = [_tree("https://a.test/", "[9]<a>Home />")]
    with _action_ctx(helpers, trees):
        json.loads(server.browser_tabs())
        result = json.loads(server.browser_click(1, 2, observe="full"))
    assert result["ok"] is True
    assert result["observation"]["mode"] == "full"
    assert "[9]<a>Home />" in result["observation"]["tree"]


def test_input_and_select_settle_once() -> None:
    helpers = _helpers()
    trees = [
        _tree("https://a.test/", "[0]<input />"),
        _tree("https://a.test/", "[0]<input />"),
        _tree("https://a.test/", "[0]<input />"),
        _tree("https://a.test/", "[0]<input />"),
    ]
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "settle", return_value={"quiet": True, "navigated": False}) as settle,
        patch.object(dom_indexing, "get_elements", side_effect=trees),
        patch.object(dom_indexing, "_register_driver_for_new_documents"),
        patch.object(
            dom_indexing,
            "fill",
            return_value={"ok": True, "tagName": "input", "mode_used": "fast"},
        ),
        patch.object(dom_indexing, "select_option", return_value=True),
    ):
        json.loads(server.browser_tabs())
        json.loads(server.browser_get_elements())
        json.loads(server.browser_input_by_index(0, "hi", observe="diff"))
        fills = settle.call_count
        json.loads(server.browser_select_by_index(0, "A", observe="diff"))
        json.loads(server.browser_fill("#x", "y", observe="diff"))
    assert fills == 1
    assert settle.call_count == 3


def test_press_and_scroll_include_observation() -> None:
    helpers = _helpers()
    trees = [
        _tree("https://a.test/", "[0]<a>x />"),
        _tree("https://a.test/", "[1]<a>y />"),
    ]
    with _action_ctx(helpers, trees):
        json.loads(server.browser_tabs())
        press = json.loads(server.browser_press_key("Enter", observe="full"))
        scroll = json.loads(server.browser_scroll(0, 0, observe="full"))
    assert press["action"]["type"] == "press"
    assert press["observation"]["mode"] == "full"
    assert scroll["action"]["type"] == "scroll"


def test_goto_defaults_to_full_observation() -> None:
    helpers = _helpers()
    helpers.goto_url = MagicMock()
    trees = [_tree("https://a.test/next", "[0]<a>n />")]
    with _action_ctx(helpers, trees):
        json.loads(server.browser_tabs())
        result = json.loads(server.browser_goto("https://a.test/next"))
    assert result["playbooks"] == []
    assert result["action"]["type"] == "goto"
    assert result["observation"]["mode"] == "full"
    assert "[0]<a>n />" in result["observation"]["tree"]


def test_goto_explicit_diff_allowed() -> None:
    helpers = _helpers()
    helpers.goto_url = MagicMock()
    trees = [
        _tree("https://a.test/", "[0]<a>old />"),
        _tree("https://a.test/next", "[1]<a>new />"),
    ]
    with _action_ctx(helpers, trees):
        json.loads(server.browser_tabs())
        json.loads(server.browser_get_elements())
        result = json.loads(server.browser_goto("https://a.test/next", observe="diff"))
    assert result["observation"]["mode"] == "full"
    assert "[1]<a>new />" in result["observation"]["tree"]


def test_oversized_diff_collapses_to_full() -> None:
    helpers = _helpers()
    previous = "\n".join(f"[{i}]<a>old{i} />" for i in range(10))
    current = "\n".join(f"[{i}]<a>new{i} />" for i in range(10))
    trees = [_tree("https://a.test/", previous), _tree("https://a.test/", current)]
    with _action_ctx(helpers, trees):
        json.loads(server.browser_tabs())
        json.loads(server.browser_get_elements())
        result = json.loads(server.browser_click_by_index(0, observe="diff"))
    assert result["observation"]["mode"] == "full"
    assert "new0" in result["observation"]["tree"]


def test_navigation_re_injects_and_uses_full_tree() -> None:
    helpers = _helpers()
    trees = [_tree("https://a.test/two", "[9]<a>new />")]
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing, "settle", return_value={"quiet": True, "navigated": True}
        ),
        patch.object(dom_indexing, "get_elements", side_effect=trees),
        patch.object(dom_indexing, "_register_driver_for_new_documents") as register,
        patch.object(
            dom_indexing, "get_rect", return_value={"x": 1, "y": 2, "tagName": "a"}
        ),
    ):
        json.loads(server.browser_tabs())
        result = json.loads(server.browser_click_by_index(9, observe="diff"))
    assert result["page"]["navigated"] is True
    assert result["observation"]["mode"] == "full"
    assert "[9]<a>new />" in result["observation"]["tree"]
    register.assert_called()


def test_click_retries_without_prior_browser_tabs() -> None:
    helpers = _helpers()
    same = _tree("https://a.test/", "[0]<button>Next />")
    changed = _tree("https://a.test/", "[0]<button>Next />\n[1]<p>page-2 />")
    trees = [same, same, changed]
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "settle", return_value={"quiet": True, "navigated": False}) as settle,
        patch.object(dom_indexing, "get_elements", side_effect=trees),
        patch.object(dom_indexing, "_register_driver_for_new_documents"),
        patch.object(
            dom_indexing,
            "get_rect",
            return_value={"x": 10, "y": 20, "tagName": "button"},
        ),
    ):
        json.loads(server.browser_get_elements())
        result = json.loads(server.browser_click_by_index(0, observe="diff"))
    assert settle.call_count >= 2
    assert any("page-2" in line for line in result["observation"]["added"])


def test_click_retries_until_tree_changes() -> None:
    helpers = _helpers()
    same = _tree("https://a.test/", "[0]<button>Next />")
    changed = _tree("https://a.test/", "[0]<button>Next />\n[1]<p>page-2 />")
    trees = [same, same, changed]
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "settle", return_value={"quiet": True, "navigated": False}) as settle,
        patch.object(dom_indexing, "get_elements", side_effect=trees),
        patch.object(dom_indexing, "_register_driver_for_new_documents"),
        patch.object(
            dom_indexing,
            "get_rect",
            return_value={"x": 10, "y": 20, "tagName": "button"},
        ),
    ):
        json.loads(server.browser_tabs())
        json.loads(server.browser_get_elements())
        result = json.loads(server.browser_click_by_index(0, observe="diff"))
    assert settle.call_count >= 2
    assert result["observation"]["mode"] == "diff"
    assert any("page-2" in line for line in result["observation"]["added"])


def test_invalid_action_observe() -> None:
    helpers = _helpers()
    with _patch_harness(helpers):
        result = json.loads(server.browser_click_by_index(1, observe="nope"))
    assert result["ok"] is False
    assert "unknown observe" in result["error"]


def test_settled_false_when_quiet_times_out() -> None:
    helpers = _helpers()
    trees = [_tree("https://a.test/", "[0]<a>x />")]
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing, "settle", return_value={"quiet": False, "navigated": False}
        ),
        patch.object(dom_indexing, "get_elements", side_effect=trees),
        patch.object(dom_indexing, "get_rect", return_value={"x": 1, "y": 2}),
    ):
        json.loads(server.browser_tabs())
        result = json.loads(server.browser_click_by_index(0, observe="full"))
    assert result["page"]["settled"] is False
    assert result["observation"]["settled"] is False


def test_switch_tab_includes_observation() -> None:
    helpers = _helpers()
    trees = [_tree("https://a.test/", "[0]<a>x />")]
    with _action_ctx(helpers, trees):
        json.loads(server.browser_tabs())
        result = json.loads(server.browser_switch_tab("t1", observe="full"))
    assert result["ok"] is True
    assert result["action"]["type"] == "switch_tab"
    assert result["observation"]["mode"] == "full"
