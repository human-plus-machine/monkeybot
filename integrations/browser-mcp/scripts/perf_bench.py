#!/usr/bin/env python3
"""In-process baseline bench for browser-mcp tools. Not part of pytest.

Serves tests/fixtures over HTTP and drives MCP tools against a local Chrome
attached via BU_CDP_URL. Prints median wall time, harness calls, and result
size per tool plus tool-calls-per-scenario.

Usage:
    BROWSER_MCP_PERF=1 BU_CDP_URL=http://127.0.0.1:9222 \\
        uv run python scripts/perf_bench.py
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import statistics
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

_INDEX_RE = re.compile(r"\[(\d+)\]")
_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
_RUNS = 3
_FORM_FIELDS = ("Full name", "Email", "Phone", "Address", "City", "Nickname")


def _require_cdp() -> str:
    url = (os.environ.get("BU_CDP_URL") or os.environ.get("BU_CDP_WS") or "").strip()
    if not url:
        print(
            "perf_bench: set BU_CDP_URL (or BU_CDP_WS) to a live Chrome DevTools endpoint.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return url


def _start_server() -> tuple[ThreadingHTTPServer, str]:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(_FIXTURES), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    return httpd, f"http://{host}:{port}"


def _parse(result: str) -> dict[str, Any]:
    payload = json.loads(result)
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected JSON object, got {type(payload).__name__}")
    return payload


def _tree_index(
    tree: str, contains: str, *, prefer_tags: tuple[str, ...] = ()
) -> int:
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


def _new_records(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    data = path.read_bytes() if path.exists() else b""
    chunk = data[offset:].decode("utf-8")
    records = [json.loads(line) for line in chunk.splitlines() if line.strip()]
    return records, len(data)


def _run_form(base: str) -> None:
    from browser_mcp import server

    server.browser_goto(f"{base}/form.html")
    # Form fields near the bottom sit below a typical viewport; this scenario
    # still needs the full tree to fill Nickname and click Submit.
    payload = _parse(server.browser_get_elements(viewport_only=False))
    tree = str(payload.get("tree") or "")
    for label in _FORM_FIELDS:
        idx = _tree_index(tree, label, prefer_tags=("input", "textarea"))
        server.browser_input_by_index(idx, "benchvalue")
    # Submit starts disabled (unindexed) until Nickname is filled.
    tree = str(_parse(server.browser_get_elements(viewport_only=False)).get("tree") or "")
    server.browser_click_by_index(_tree_index(tree, "Submit", prefer_tags=("button",)))
    server.browser_get_elements(viewport_only=False)


def _run_long_list(base: str) -> None:
    from browser_mcp import server

    server.browser_goto(f"{base}/long_list.html")
    server.browser_get_elements()


def _run_spa(base: str) -> None:
    from browser_mcp import server

    server.browser_goto(f"{base}/spa.html")
    payload = _parse(server.browser_get_elements())
    tree = str(payload.get("tree") or "")
    server.browser_click_by_index(_tree_index(tree, "Next", prefer_tags=("button",)))
    server.browser_get_elements()


def _run_spa_wait(base: str) -> None:
    from browser_mcp import server

    server.browser_goto(f"{base}/spa.html")
    payload = _parse(server.browser_get_elements())
    tree = str(payload.get("tree") or "")
    server.browser_click_by_index(_tree_index(tree, "Next", prefer_tags=("button",)))
    server.browser_wait_for("#page-2")


def _run_compare_three(base: str) -> None:
    from browser_mcp import server, tabs

    helpers, _ = server._browser_harness()
    reg = tabs.registry()
    with contextlib.suppress(Exception):
        reg.refresh(helpers)
        focused = reg.focused()
        for state in list(reg.tabs()):
            if focused is None or state.target_id == focused.target_id:
                continue
            with contextlib.suppress(Exception):
                server._close_target(helpers, state.target_id)
        reg.refresh(helpers)

    server.browser_goto(f"{base}/form.html")
    server.browser_open_tab(f"{base}/long_list.html", focus=False)
    server.browser_open_tab(f"{base}/spa.html", focus=False)
    payload = _parse(server.browser_read_tabs())
    if not payload.get("ok"):
        raise RuntimeError(f"read_tabs failed: {payload}")
    if len(payload.get("tabs") or []) < 2:
        raise RuntimeError(f"expected at least 2 tabs in read_tabs, got {payload}")


_shot_bytes: dict[str, int] = {}


def _run_screenshot(base: str) -> None:
    from browser_mcp import server

    server.browser_goto(f"{base}/long_list.html")
    server.browser_get_elements()
    png = _parse(server.browser_screenshot(format="png", max_dim=1800))
    jpg = _parse(server.browser_screenshot())
    _shot_bytes["png"] = int(png.get("bytes") or 0)
    _shot_bytes["jpeg"] = int(jpg.get("bytes") or 0)


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _print_scenario(name: str, runs: list[tuple[list[dict[str, Any]], float]]) -> None:
    by_tool: dict[str, dict[str, list[float]]] = {}
    tool_counts: list[int] = []
    totals: list[float] = []
    for records, elapsed_ms in runs:
        tool_counts.append(len(records))
        totals.append(elapsed_ms)
        for rec in records:
            slot = by_tool.setdefault(
                str(rec["tool"]),
                {"wall_ms": [], "harness_calls": [], "result_chars": []},
            )
            slot["wall_ms"].append(float(rec["wall_ms"]))
            slot["harness_calls"].append(float(rec["harness_calls"]))
            slot["result_chars"].append(float(rec["result_chars"]))

    print(f"=== {name} ===")
    print(
        f"{'tool':<28} {'median_wall_ms':>14} {'harness_calls':>14} {'result_chars':>14}"
    )
    for tool, slot in by_tool.items():
        print(
            f"{tool:<28} {_median(slot['wall_ms']):>14.1f} "
            f"{_median(slot['harness_calls']):>14.1f} {_median(slot['result_chars']):>14.0f}"
        )
    print(f"tool_calls_per_scenario: {tool_counts[0] if tool_counts else 0}")
    print(f"total_scenario_ms (median of {_RUNS}): {_median(totals):.1f}")
    print()


def main() -> int:
    _require_cdp()
    os.environ["BROWSER_MCP_PERF"] = "1"
    log_dir = Path(tempfile.mkdtemp(prefix="browser-mcp-perf-"))
    log_path = log_dir / "tools.jsonl"
    os.environ["BROWSER_MCP_PERF_LOG"] = str(log_path)
    os.environ.setdefault("MONKEYBOT_WORKSPACE_ROOT", str(log_dir / "workspace"))

    httpd, base = _start_server()
    scenarios: list[tuple[str, Callable[[str], None]]] = [
        ("form.html", _run_form),
        ("long_list.html", _run_long_list),
        ("spa.html", _run_spa),
        ("spa_wait", _run_spa_wait),
        ("compare_three", _run_compare_three),
        ("screenshot (long_list.html)", _run_screenshot),
    ]
    try:
        offset = 0
        for name, run in scenarios:
            runs: list[tuple[list[dict[str, Any]], float]] = []
            for _ in range(_RUNS):
                started = time.perf_counter()
                run(base)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                records, offset = _new_records(log_path, offset)
                if not records:
                    print(f"perf_bench: no perf records for {name}", file=sys.stderr)
                    return 1
                failed = [r for r in records if not r.get("ok", True)]
                if failed:
                    print(
                        f"perf_bench: tool failure in {name}: {failed[0]}",
                        file=sys.stderr,
                    )
                    return 1
                runs.append((records, elapsed_ms))
            _print_scenario(name, runs)
            if name == "screenshot (long_list.html)":
                png_b = _shot_bytes.get("png") or 0
                jpg_b = _shot_bytes.get("jpeg") or 0
                pct = (1 - jpg_b / png_b) * 100 if png_b else 0
                print(f"screenshot_png_bytes (max_dim=1800): {png_b}")
                print(
                    f"screenshot_jpeg_bytes (max_dim=1200, q=60): {jpg_b}  (−{pct:.0f} %)"
                )
                print()
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
