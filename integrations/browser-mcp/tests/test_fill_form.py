"""Unit tests for fill_form, click_text, and extract contracts."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from browser_mcp import actions, dom_indexing, server, tabs


@pytest.fixture(autouse=True)
def _reset() -> None:
    original = server._bh
    original_bound = server._bound_cdp
    server._bh = None
    server._bound_cdp = None
    dom_indexing.clear_registered_targets()
    tabs.reset_registry()
    yield
    server._bh = original
    server._bound_cdp = original_bound
    dom_indexing.clear_registered_targets()
    tabs.reset_registry()


def _patch_harness(helpers: MagicMock):
    return patch.object(server, "_browser_harness", return_value=(helpers, MagicMock()))


def _helpers() -> MagicMock:
    helpers = MagicMock()
    row = {
        "targetId": "aaa",
        "target_id": "aaa",
        "url": "https://a.test/",
        "title": "A",
    }
    helpers.list_tabs.return_value = [row]
    helpers.current_tab.return_value = dict(row)
    helpers.page_info.return_value = {"url": "https://a.test/", "title": "A"}
    helpers.js.return_value = True
    return helpers


def test_fill_form_unresolved_is_not_error() -> None:
    helpers = _helpers()
    with (
        _patch_harness(helpers),
        patch.object(
            actions,
            "do_fill_form",
            return_value={
                "ok": True,
                "filled": [{"label": "Email", "index": 2, "how": "aria-label"}],
                "unresolved": ["Promo code"],
                "submitted": False,
            },
        ),
    ):
        json.loads(server.browser_tabs())
        result = json.loads(
            server.browser_fill_form(
                {"Email": "a@b.test", "Promo code": "x"}, observe="none"
            )
        )
    assert result["ok"] is True
    assert result["unresolved"] == ["Promo code"]
    assert result["filled"][0]["how"] == "aria-label"


def test_fill_form_all_failed_is_error() -> None:
    helpers = _helpers()
    with (
        _patch_harness(helpers),
        patch.object(
            actions,
            "do_fill_form",
            return_value={
                "ok": False,
                "filled": [],
                "unresolved": ["Nope"],
                "submitted": False,
            },
        ),
    ):
        json.loads(server.browser_tabs())
        result = json.loads(server.browser_fill_form({"Nope": "x"}, observe="none"))
    assert result["ok"] is False
    assert result["unresolved"] == ["Nope"]


def test_click_text_near_miss_shape() -> None:
    helpers = _helpers()
    with (
        _patch_harness(helpers),
        patch.object(
            actions,
            "do_click_text",
            return_value={
                "ok": False,
                "error": "no matching element for 'Nope'",
                "did_you_mean": [
                    {"text": "Submit", "role": "button", "tagName": "button"}
                ],
            },
        ),
    ):
        json.loads(server.browser_tabs())
        result = json.loads(server.browser_click_text("Nope", observe="none"))
    assert result["ok"] is False
    assert result["did_you_mean"][0]["text"] == "Submit"


def test_extract_href_attribute() -> None:
    helpers = _helpers()
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing,
            "extract_rows",
            return_value={
                "rows": [{"title": "Oak desk", "href": "/desk"}],
                "truncated": False,
            },
        ) as extract_rows,
    ):
        json.loads(server.browser_tabs())
        result = json.loads(
            server.browser_extract(".card", {"title": "h2", "href": "a@href"})
        )
    assert result == {
        "ok": True,
        "rows": [{"title": "Oak desk", "href": "/desk"}],
        "truncated": False,
    }
    extract_rows.assert_called_once()
    assert extract_rows.call_args.args[1] == ".card"
    assert extract_rows.call_args.args[2] == {"title": "h2", "href": "a@href"}
