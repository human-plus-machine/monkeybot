"""Live-browser checks for Phase 3 observation diet.

Skipped unless ``BROWSER_MCP_INTEGRATION=1``.
"""

from __future__ import annotations

import json
import os
import re

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BROWSER_MCP_INTEGRATION") != "1",
    reason="set BROWSER_MCP_INTEGRATION=1 to run live browser tests",
)

_INTERACTIVE = re.compile(r"^\s*\[\d+\]")


def _interactive_lines(tree: str) -> list[str]:
    return [ln for ln in tree.splitlines() if _INTERACTIVE.match(ln)]


def test_viewport_default_and_contains_on_long_list(
    fixture_server: str, cdp_url: str
) -> None:
    from browser_mcp import server

    goto = json.loads(server.browser_goto(f"{fixture_server}/long_list.html"))
    assert "long_list.html" in str(goto.get("url") or "")
    payload = json.loads(server.browser_get_elements())
    assert payload.get("ok") is True
    tree = str(payload.get("tree") or "")
    assert len(_interactive_lines(tree)) < 150
    assert int(payload.get("below_viewport") or 0) > 0
    assert "below the viewport" in tree
    assert payload.get("truncated") is False

    buy = json.loads(server.browser_get_elements(contains="Buy"))
    assert buy.get("ok") is True
    buy_lines = [ln for ln in str(buy.get("tree") or "").splitlines() if "Buy now" in ln]
    assert len(buy_lines) == 3
    assert not any("Link " in ln for ln in buy_lines)


def test_index_stable_after_inserting_nodes(
    fixture_server: str, cdp_url: str, tree_index
) -> None:
    from browser_mcp import server

    json.loads(server.browser_goto(f"{fixture_server}/long_list.html"))
    first = json.loads(
        server.browser_get_elements(viewport_only=False, max_elements=1000)
    )
    idx = tree_index(str(first.get("tree") or ""), "Button 0", prefer_tags=("button",))
    injected = json.loads(
        server.browser_js(
            "(() => {"
            "const sec = document.querySelector('section');"
            "for (let i = 0; i < 50; i++) {"
            "const b = document.createElement('button');"
            "b.textContent = 'Inserted ' + i;"
            "sec.insertBefore(b, sec.firstChild);"
            "}"
            "return 50;"
            "})()"
        )
    )
    assert injected.get("result") == 50
    second = json.loads(
        server.browser_get_elements(viewport_only=False, max_elements=1000)
    )
    idx2 = tree_index(str(second.get("tree") or ""), "Button 0", prefer_tags=("button",))
    assert idx2 == idx
    inserted = tree_index(
        str(second.get("tree") or ""), "Inserted 0", prefer_tags=("button",)
    )
    assert inserted != idx


def test_get_text_returns_article_body_only(
    fixture_server: str, cdp_url: str
) -> None:
    from browser_mcp import server

    json.loads(server.browser_goto(f"{fixture_server}/article.html"))
    payload = json.loads(server.browser_get_text())
    assert payload.get("ok") is True
    text = str(payload.get("text") or "")
    assert "City Council Approves River Walk" in text
    assert "two-mile river walk" in text
    assert "newsletter" not in text
    assert "Copyright" not in text
    assert "do-not-index" not in text
    assert "Home" not in text
