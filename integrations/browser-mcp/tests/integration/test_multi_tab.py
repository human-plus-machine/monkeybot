"""Live-browser checks for Phase 2 multi-tab reads and focus rules.

Skipped unless ``BROWSER_MCP_INTEGRATION=1``.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright")

pytestmark = pytest.mark.skipif(
    os.environ.get("BROWSER_MCP_INTEGRATION") != "1",
    reason="set BROWSER_MCP_INTEGRATION=1 to run live browser tests",
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _wait_cdp(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/json/version", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:
            last = str(exc)
        time.sleep(0.1)
    raise RuntimeError(f"CDP endpoint {url} never became ready: {last}")


@pytest.fixture
def fixture_server() -> str:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(_FIXTURES), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def cdp_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    from playwright.sync_api import sync_playwright

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--disable-gpu",
        ],
    )
    _wait_cdp(url)
    log = tmp_path / "tools.jsonl"
    monkeypatch.setenv("BU_CDP_URL", url)
    monkeypatch.delenv("BU_CDP_WS", raising=False)
    monkeypatch.delenv("BROWSER_BACKEND", raising=False)
    monkeypatch.setenv("BROWSER_MCP_PERF", "1")
    monkeypatch.setenv("BROWSER_MCP_PERF_LOG", str(log))
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    from browser_mcp import backend, dom_indexing, server

    backend._bh = None
    backend._bound_cdp = None
    dom_indexing.clear_registered_targets()
    try:
        yield url
    finally:
        try:
            backend.teardown_bound_backend()
        except Exception:
            pass
        backend._bh = None
        backend._bound_cdp = None
        dom_indexing.clear_registered_targets()
        browser.close()
        playwright.stop()


def _last_record(path: Path) -> dict[str, Any]:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, f"no perf records in {path}"
    return json.loads(lines[-1])


def test_background_read_does_not_steal_focus_then_action_does(
    fixture_server: str, cdp_url: str, tmp_path: Path
) -> None:
    from browser_mcp import server

    log = tmp_path / "tools.jsonl"
    base = fixture_server
    json.loads(server.browser_goto(f"{base}/form.html"))
    listed = json.loads(server.browser_tabs())
    focused_before = listed["focused"]
    opened = json.loads(
        server.browser_open_tab(f"{base}/long_list.html", alias="list", focus=False)
    )
    assert opened.get("ok") is True
    t2 = opened["tab"]

    listed = json.loads(server.browser_tabs())
    assert listed["focused"] == focused_before

    elements = json.loads(server.browser_get_elements(tab=t2))
    assert elements.get("ok") is True
    assert "Link 0" in str(elements.get("tree") or "")
    rec = _last_record(log)
    assert rec["tool"] == "browser_get_elements"
    assert rec["harness_calls"] >= 1

    listed = json.loads(server.browser_tabs())
    assert listed["focused"] == focused_before

    tree = str(elements.get("tree") or "")
    idx = 0
    for line in tree.splitlines():
        if "Link 0" in line:
            start = line.find("[")
            end = line.find("]")
            idx = int(line[start + 1 : end])
            break
    clicked = json.loads(server.browser_click_by_index(idx, tab=t2))
    assert clicked.get("ok") is True
    listed = json.loads(server.browser_tabs())
    assert listed["focused"] == t2

    read = json.loads(server.browser_read_tabs())
    assert read.get("ok") is True
    assert len(read.get("tabs") or []) >= 1

    closed = json.loads(server.browser_close_tab(t2))
    assert closed.get("ok") is True
    listed = json.loads(server.browser_tabs())
    assert listed["focused"] == focused_before
