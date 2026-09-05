"""Shared live-browser fixtures for tests/integration/.

Skipped at the test-module layer unless ``BROWSER_MCP_INTEGRATION=1``.
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

_INDEX_RE = re.compile(r"\[(\d+)\]")
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def tree_index():
    return _tree_index


@pytest.fixture
def last_perf_record():
    return _last_perf_record


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


def _last_perf_record(path: Path) -> dict[str, Any]:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, f"no perf records in {path}"
    return json.loads(lines[-1])


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
    pytest.importorskip("playwright")
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
    from browser_mcp import backend, dom_indexing, server, tabs

    backend._bh = None
    backend._bound_cdp = None
    dom_indexing.clear_registered_targets()
    tabs.reset_registry()
    try:
        yield url
    finally:
        with contextlib.suppress(Exception):
            backend.teardown_bound_backend()
        backend._bh = None
        backend._bound_cdp = None
        dom_indexing.clear_registered_targets()
        tabs.reset_registry()
        browser.close()
        playwright.stop()
