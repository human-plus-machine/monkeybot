"""Live-browser checks for Phase 1 driver persistence and fast fill.

Skipped unless ``BROWSER_MCP_INTEGRATION=1``. Needs Playwright's Chromium and
the browser-harness daemon (same stack as ``scripts/perf_bench.py``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BROWSER_MCP_INTEGRATION") != "1",
    reason="set BROWSER_MCP_INTEGRATION=1 to run live browser tests",
)


def test_fast_fill_enables_submit_and_driver_survives_navigation(
    fixture_server: str, cdp_url: str, tmp_path: Path, tree_index, last_perf_record
) -> None:
    from browser_mcp import server

    log = tmp_path / "tools.jsonl"
    base = fixture_server
    goto = json.loads(server.browser_goto(f"{base}/form.html"))
    assert "form.html" in str(goto.get("url") or "")

    payload = json.loads(server.browser_get_elements(contains="Nickname"))
    assert payload.get("ok") is True
    tree = str(payload.get("tree") or "")
    nick = tree_index(tree, "Nickname", prefer_tags=("input",))
    filled = json.loads(
        server.browser_input_by_index(nick, "fastnick", mode="fast")
    )
    assert filled.get("ok") is True
    assert filled.get("mode_used") == "fast"

    value = json.loads(server.browser_js("document.getElementById('nickname').value"))
    assert value.get("result") == "fastnick"

    tree = str(json.loads(server.browser_get_elements(contains="Submit")).get("tree") or "")
    assert "Submit" in tree

    server.browser_goto(f"{base}/long_list.html")
    second = json.loads(server.browser_get_elements())
    assert second.get("ok") is True
    assert "Link 0" in str(second.get("tree") or "")
    rec = last_perf_record(log)
    assert rec["tool"] == "browser_get_elements"
    assert rec["harness_calls"] == 1
    assert rec["ok"] is True
