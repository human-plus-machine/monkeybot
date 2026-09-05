"""Live-browser checks for fill_form, click_text, act, and extract.

Skipped unless ``BROWSER_MCP_INTEGRATION=1``.
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BROWSER_MCP_INTEGRATION") != "1",
    reason="set BROWSER_MCP_INTEGRATION=1 to run live browser tests",
)

_FORM_LABELS = {
    "Full name": "label_for",
    "Email": "aria-label",
    "Phone": "aria-labelledby",
    "Address": "placeholder",
    "City": "name",
    "Country": "label_for",
    "Zip": "id",
    "Company": "preceding_text",
    "Role": "label_for",
    "Website": "label_for",
    "Comments": "label_for",
    "Nickname": "label_for",
}


def test_fill_form_resolves_strategies_and_submit(fixture_server: str, cdp_url: str) -> None:
    from browser_mcp import server

    json.loads(server.browser_goto(f"{fixture_server}/form.html"))
    fields = {label: "benchvalue" for label in _FORM_LABELS}
    fields["Country"] = "United States"
    fields["Promo code"] = "nope"
    result = json.loads(
        server.browser_act(
            [{"do": "fill_form", "fields": fields, "submit": True}],
            observe="full",
        )
    )
    assert result["ok"] is True
    filled_row = result["steps"][0]
    how_by_label = {row["label"]: row["how"] for row in filled_row["filled"]}
    for label, how in _FORM_LABELS.items():
        assert how_by_label.get(label) == how, (label, how_by_label)
    assert "Promo code" in filled_row["unresolved"]
    assert filled_row["submitted"] is True
    status = json.loads(server.browser_js("!document.getElementById('status').hidden"))
    assert status.get("result") is True


def test_click_text_prefers_button_role(fixture_server: str, cdp_url: str) -> None:
    from browser_mcp import server

    json.loads(server.browser_goto(f"{fixture_server}/form.html"))
    json.loads(
        server.browser_act(
            [{"do": "fill_form", "fields": {"Nickname": "clicknick"}}],
            observe="none",
        )
    )
    result = json.loads(
        server.browser_click_text("Submit", role="button", observe="full")
    )
    assert result["ok"] is True
    assert result["clicked"]["tagName"] == "button"
    status = json.loads(server.browser_js("!document.getElementById('status').hidden"))
    assert status.get("result") is True


def test_act_input_then_click_text(fixture_server: str, cdp_url: str) -> None:
    from browser_mcp import server

    json.loads(server.browser_goto(f"{fixture_server}/form.html"))
    filled = json.loads(
        server.browser_act(
            [{"do": "fill_form", "fields": {"Nickname": "actnick"}}],
            observe="none",
        )
    )
    nick = filled["steps"][0]["filled"][0]["index"]
    result = json.loads(
        server.browser_act(
            [
                {"do": "input", "index": nick, "text": "actnick2"},
                {"do": "click_text", "text": "Submit", "role": "button"},
            ],
            observe="full",
        )
    )
    assert result["ok"] is True
    assert len(result["steps"]) == 2
    status = json.loads(server.browser_js("!document.getElementById('status').hidden"))
    assert status.get("result") is True


def test_act_failed_step_returns_observation(fixture_server: str, cdp_url: str) -> None:
    from browser_mcp import server

    json.loads(server.browser_goto(f"{fixture_server}/form.html"))
    result = json.loads(
        server.browser_act(
            [
                {"do": "click_text", "text": "Submit", "role": "button"},
                {"do": "click", "index": 99999},
            ],
            observe="full",
        )
    )
    assert result["ok"] is False
    assert result["failed_step"] == 1
    assert result.get("observation")


def test_extract_cards(fixture_server: str, cdp_url: str) -> None:
    from browser_mcp import server

    json.loads(server.browser_goto(f"{fixture_server}/cards.html"))
    result = json.loads(
        server.browser_extract(".card", {"title": "h2", "price": ".price", "href": "a@href"})
    )
    assert result["ok"] is True
    assert result["truncated"] is False
    assert result["rows"] == [
        {"title": "Oak desk", "price": "$240", "href": "/desk"},
        {"title": "Pine chair", "price": "$80", "href": "/chair"},
        {"title": "Wool lamp", "price": "$45", "href": "/lamp"},
    ]
