"""Unit tests for browser_act validation, stop-on-error, and shared do_* helpers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from browser_mcp import actions, backend, dom_indexing, login, server, tabs


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


def _helpers(*, url: str = "https://a.test/") -> MagicMock:
    helpers = MagicMock()
    row = {"targetId": "aaa", "target_id": "aaa", "url": url, "title": "A"}
    helpers.list_tabs.return_value = [row]
    helpers.current_tab.return_value = dict(row)
    helpers.page_info.return_value = {"url": url, "title": "A", "w": 800, "h": 600}
    helpers.js.return_value = True
    helpers.switch_tab.return_value = "sid"
    return helpers


def test_act_rejects_malformed_step() -> None:
    result = json.loads(server.browser_act([{"do": "click"}]))
    assert result["ok"] is False
    assert result["step_index"] == 0
    assert "index" in result["error"]


def test_act_rejects_unknown_do() -> None:
    result = json.loads(server.browser_act([{"do": "explode"}]))
    assert result["ok"] is False
    assert "unknown do" in result["error"]


def test_act_rejects_over_cap() -> None:
    steps = [{"do": "settle"}] * (actions.MAX_ACT_STEPS + 1)
    result = json.loads(server.browser_act(steps))
    assert result["ok"] is False
    assert result["step_index"] == actions.MAX_ACT_STEPS
    assert "too many steps" in result["error"]


def test_click_by_index_uses_shared_do() -> None:
    helpers = _helpers()
    with (
        _patch_harness(helpers),
        patch.object(
            actions,
            "do_click_by_index",
            return_value={"ok": True, "clicked": {"x": 1, "y": 2}},
        ) as do_click,
    ):
        json.loads(server.browser_tabs())
        result = json.loads(server.browser_click_by_index(9, observe="none"))
    assert result == {"ok": True, "clicked": {"x": 1, "y": 2}}
    do_click.assert_called_once()
    assert do_click.call_args.args[1] == 9


def test_act_stop_on_error_returns_completed_and_observation() -> None:
    helpers = _helpers()
    tree = {
        "tree": "[0]<button>A />",
        "elementCount": 1,
        "url": "https://a.test/",
        "title": "A",
        "truncated": False,
        "below_viewport": 0,
        "omitted": 0,
    }
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "settle", return_value={"quiet": True, "navigated": False}),
        patch.object(dom_indexing, "get_elements", return_value=tree),
        patch.object(
            actions,
            "do_click_by_index",
            side_effect=[
                {"ok": True, "clicked": {"x": 1, "y": 2}},
                dom_indexing.ElementNotFoundError("Element index not found."),
            ],
        ),
    ):
        json.loads(server.browser_tabs())
        json.loads(server.browser_get_elements())
        result = json.loads(
            server.browser_act(
                [{"do": "click", "index": 0}, {"do": "click", "index": 99}],
                observe="diff",
            )
        )
    assert result["ok"] is False
    assert result["failed_step"] == 1
    assert result["completed"][0]["do"] == "click"
    assert result["completed"][0]["ok"] is True
    assert "Element index not found" in result["error"]
    assert result["observation"]["mode"] in {"diff", "full"}


def test_act_observe_none_skips_snapshot() -> None:
    helpers = _helpers()
    with (
        _patch_harness(helpers),
        patch.object(
            actions, "do_press", return_value={"ok": True, "key": "Enter", "modifiers": 0}
        ),
        patch.object(dom_indexing, "settle") as settle,
        patch.object(dom_indexing, "get_elements") as get_elements,
    ):
        json.loads(server.browser_tabs())
        result = json.loads(
            server.browser_act([{"do": "press", "key": "Enter"}], observe="none")
        )
    assert result["ok"] is True
    assert result["steps"][0]["do"] == "press"
    assert "observation" not in result
    get_elements.assert_not_called()
    settle.assert_called_once()


def test_act_rejects_fill_form_without_fields() -> None:
    result = json.loads(server.browser_act([{"do": "fill_form"}]))
    assert result["ok"] is False
    assert "fields" in result["error"]


def test_act_login_requires_expected_origin() -> None:
    result = json.loads(server.browser_act([{"do": "login"}]))
    assert result["ok"] is False
    assert "expected_origin" in result["error"]


def test_act_login_calls_sealed_login() -> None:
    helpers = _helpers()
    with (
        _patch_harness(helpers),
        patch.object(
            login,
            "_sealed_login",
            return_value={"ok": True, "loggedIn": True, "origin": "https://a.test"},
        ) as sealed,
        patch.object(dom_indexing, "settle", return_value={"quiet": True, "navigated": False}),
        patch.object(dom_indexing, "get_elements") as get_elements,
    ):
        json.loads(server.browser_tabs())
        result = json.loads(
            server.browser_act(
                [{"do": "login", "expected_origin": "https://a.test", "username": "ada"}],
                observe="none",
            )
        )
    assert result["ok"] is True
    sealed.assert_called_once_with("ada", "https://a.test")
    get_elements.assert_not_called()


def test_act_fill_form_uses_shared_do() -> None:
    helpers = _helpers()
    with (
        _patch_harness(helpers),
        patch.object(
            actions,
            "do_fill_form",
            return_value={"ok": True, "filled": [{"label": "Email", "index": 1, "how": "aria-label"}], "unresolved": [], "submitted": False},
        ) as do_fill,
        patch.object(dom_indexing, "settle", return_value={"quiet": True, "navigated": False}),
        patch.object(dom_indexing, "get_elements") as get_elements,
    ):
        json.loads(server.browser_tabs())
        result = json.loads(
            server.browser_act(
                [{"do": "fill_form", "fields": {"Email": "a@b.c"}}],
                observe="none",
            )
        )
    assert result["ok"] is True
    do_fill.assert_called_once()
    get_elements.assert_not_called()
