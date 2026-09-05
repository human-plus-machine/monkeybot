"""Live-browser checks for Phase 1 driver persistence and fast fill.

Skipped unless ``BROWSER_MCP_INTEGRATION=1``. Needs Playwright's Chromium and
the browser-harness daemon (same stack as ``scripts/perf_bench.py``).
"""

from __future__ import annotations

import json
import os
import re
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

_INDEX_RE = re.compile(r"\[(\d+)\]")
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _tree_index(tree: str, contains: str, *, prefer_tags: tuple[str, ...] = ()) -> int:
    needle = contains.lower()
    fallback: int | None = None
    for line in tree.splitlines():
        if needle not in line.lower():
            continue
        match = _INDEX_RE.search(line)
        if not match:
            continue
        idx = int(match.group(1))
        if prefer_tags and not any(f"<{tag}" in line.lower() for tag in prefer_tags):
            if fallback is None:
                fallback = idx
            continue
        return idx
    if fallback is not None:
        return fallback
    raise RuntimeError(f"no element containing {contains!r} in tree:\n{tree}")


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
    from browser_mcp import dom_indexing, server

    server._bh = None
    server._bound_cdp = None
    dom_indexing.clear_registered_targets()
    try:
        yield url
    finally:
        try:
            server._teardown_bound_backend()
        except Exception:
            pass
        server._bh = None
        server._bound_cdp = None
        dom_indexing.clear_registered_targets()
        browser.close()
        playwright.stop()


def _last_record(path: Path) -> dict[str, Any]:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, f"no perf records in {path}"
    return json.loads(lines[-1])


def test_fast_fill_enables_submit_and_driver_survives_navigation(
    fixture_server: str, cdp_url: str, tmp_path: Path
) -> None:
    from browser_mcp import server

    log = tmp_path / "tools.jsonl"
    base = fixture_server
    goto = json.loads(server.browser_goto(f"{base}/form.html"))
    assert "form.html" in str(goto.get("url") or "")

    payload = json.loads(server.browser_get_elements())
    assert payload.get("ok") is True
    tree = str(payload.get("tree") or "")
    nick = _tree_index(tree, "Nickname", prefer_tags=("input",))
    filled = json.loads(
        server.browser_input_by_index(nick, "fastnick", mode="fast")
    )
    assert filled.get("ok") is True
    assert filled.get("mode_used") == "fast"

    value = json.loads(server.browser_js("document.getElementById('nickname').value"))
    assert value.get("result") == "fastnick"

    tree = str(json.loads(server.browser_get_elements()).get("tree") or "")
    assert "Submit" in tree

    server.browser_goto(f"{base}/long_list.html")
    second = json.loads(server.browser_get_elements())
    assert second.get("ok") is True
    assert "Link 0" in str(second.get("tree") or "")
    rec = _last_record(log)
    assert rec["tool"] == "browser_get_elements"
    assert rec["harness_calls"] == 1
    assert rec["ok"] is True
