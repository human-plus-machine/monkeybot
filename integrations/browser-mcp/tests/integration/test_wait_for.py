"""Live-browser checks for Phase 8 event-driven waits.

Skipped unless ``BROWSER_MCP_INTEGRATION=1``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BROWSER_MCP_INTEGRATION") != "1",
    reason="set BROWSER_MCP_INTEGRATION=1 to run live browser tests",
)


def test_wait_for_page_2_is_one_harness_call(
    fixture_server: str, cdp_url: str, tmp_path: Path, tree_index, last_perf_record
) -> None:
    from browser_mcp import server

    log = tmp_path / "tools.jsonl"
    base = fixture_server
    json.loads(server.browser_goto(f"{base}/spa.html"))
    tree = str(json.loads(server.browser_get_elements()).get("tree") or "")
    json.loads(
        server.browser_click_by_index(
            tree_index(tree, "Next", prefer_tags=("button",)),
            observe="none",
        )
    )

    started = time.perf_counter()
    payload = json.loads(server.browser_wait_for("#page-2"))
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert payload.get("ok") is True
    assert payload.get("found") is True
    assert 200 <= elapsed_ms <= 800
    rec = last_perf_record(log)
    assert rec["tool"] == "browser_wait_for"
    assert rec["harness_calls"] == 1
    assert rec["ok"] is True


def test_wait_for_missing_selector_times_out(
    fixture_server: str, cdp_url: str
) -> None:
    from browser_mcp import server

    json.loads(server.browser_goto(f"{fixture_server}/spa.html"))
    payload = json.loads(server.browser_wait_for("#does-not-exist", timeout=0.4))
    assert payload == {"ok": False, "found": False}
