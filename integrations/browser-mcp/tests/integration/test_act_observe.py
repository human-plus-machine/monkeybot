"""Live-browser checks for Phase 4 act-then-observe.

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


def test_click_next_returns_diff_without_wait_for(
    fixture_server: str, cdp_url: str, tree_index
) -> None:
    from browser_mcp import server

    json.loads(server.browser_goto(f"{fixture_server}/spa.html"))
    got = json.loads(server.browser_get_elements())
    tree = str(got.get("tree") or "")
    idx = tree_index(tree, "Next", prefer_tags=("button",))
    clicked = json.loads(server.browser_click_by_index(idx))
    assert clicked.get("ok") is True
    obs = clicked.get("observation") or {}
    blob = "\n".join(
        [str(obs.get("tree") or "")]
        + list(obs.get("added") or [])
    )
    assert "Page 2 done" in blob or "page-2" in blob


def test_click_observe_none_skips_snapshot(
    fixture_server: str, cdp_url: str, tree_index
) -> None:
    from browser_mcp import server

    json.loads(server.browser_goto(f"{fixture_server}/spa.html"))
    tree = str(json.loads(server.browser_get_elements()).get("tree") or "")
    clicked = json.loads(
        server.browser_click_by_index(
            tree_index(tree, "Next", prefer_tags=("button",)),
            observe="none",
        )
    )
    assert clicked.get("ok") is True
    assert "observation" not in clicked
    assert "clicked" in clicked
