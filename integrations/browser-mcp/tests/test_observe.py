"""Unit tests for Phase 3 observation diet: diff, viewport default, get_text."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from browser_mcp import dom_indexing, server


@pytest.fixture(autouse=True)
def _reset() -> None:
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


def test_diff_tree_lines_added_removed_unchanged() -> None:
    previous = ["[0]<button>A />", "[1]<button>B />", "[2]<a>C />"]
    current = ["[0]<button>A />", "[3]<button>D />", "[2]<a>C />"]
    diff = dom_indexing.diff_tree_lines(previous, current)
    assert diff["unchanged"] == 2
    assert diff["removed"] == ["[1]<button>B />"]
    assert diff["added"] == ["[3]<button>D />"]


def test_attach_tree_footers() -> None:
    tree = "[0]<a>Home />"
    out = dom_indexing.attach_tree_footers(
        tree, viewport_only=True, below_viewport=12, truncated=True, omitted=4
    )
    assert tree in out
    assert "12 more interactive elements below the viewport" in out
    assert "truncated, 4 elements omitted" in out


def test_observe_diff_on_same_url() -> None:
    helpers = _helpers()
    trees = [
        _tree("https://a.test/", "[0]<button>A />\n[1]<button>B />"),
        _tree("https://a.test/", "[0]<button>A />\n[2]<button>C />"),
    ]
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "get_elements", side_effect=trees),
    ):
        json.loads(server.browser_tabs())
        first = json.loads(server.browser_get_elements())
        assert first["ok"] is True
        assert "[0]<button>A />" in first["tree"]
        second = json.loads(server.browser_get_elements(observe="diff"))
    assert second["mode"] == "diff"
    assert second["unchanged"] == 1
    assert second["added"] == ["[2]<button>C />"]
    assert second["removed"] == ["[1]<button>B />"]
    assert "tree" not in second


def test_observe_diff_falls_back_to_full_on_url_change() -> None:
    helpers = _helpers()
    trees = [
        _tree("https://a.test/one", "[0]<button>A />"),
        _tree("https://a.test/two", "[9]<a>new />"),
    ]
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "get_elements", side_effect=trees),
    ):
        json.loads(server.browser_tabs())
        json.loads(server.browser_get_elements())
        second = json.loads(server.browser_get_elements(observe="diff"))
    assert second["mode"] == "full"
    assert "[9]<a>new />" in second["tree"]


def test_viewport_env_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_MCP_VIEWPORT_DEFAULT", "0")
    helpers = _helpers()
    captured: list[bool] = []

    def fake_get(handle: object, viewport_only: bool, **_k: object) -> dict:
        captured.append(viewport_only)
        return _tree("https://a.test/", "[0]<a>x />")

    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "get_elements", side_effect=fake_get),
    ):
        json.loads(server.browser_tabs())
        json.loads(server.browser_get_elements())
    assert captured == [False]


def test_invalid_kind_and_observe() -> None:
    helpers = _helpers()
    with _patch_harness(helpers):
        json.loads(server.browser_tabs())
        kind = json.loads(server.browser_get_elements(kind="widgets"))
        observe = json.loads(server.browser_get_elements(observe="nope"))
    assert kind["ok"] is False
    assert "unknown kind" in kind["error"]
    assert observe["ok"] is False
    assert "unknown observe" in observe["error"]


def test_get_text_truncates_and_passes_selector() -> None:
    helpers = _helpers()
    seen: list[str] = []

    def js(expression: str, **_k: object) -> str:
        seen.append(expression)
        return "x" * 50

    helpers.js.side_effect = js
    with _patch_harness(helpers):
        json.loads(server.browser_tabs())
        result = json.loads(server.browser_get_text(max_chars=10, selector="article"))
    assert result["ok"] is True
    assert result["text"] == "x" * 10
    assert result["truncated"] is True
    assert result["url"] == "https://a.test/"
    assert any('"article"' in expr for expr in seen)
