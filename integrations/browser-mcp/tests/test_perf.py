"""Unit tests for opt-in browser-mcp performance instrumentation."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from browser_mcp import dom_indexing, perf, server


@pytest.fixture(autouse=True)
def _reset_perf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    original_bh = server._bh
    original_bound = server._bound_cdp
    server._bh = None
    server._bound_cdp = None
    monkeypatch.delenv("BROWSER_MCP_PERF", raising=False)
    monkeypatch.delenv("BROWSER_MCP_PERF_LOG", raising=False)
    monkeypatch.delenv("BROWSER_BACKEND", raising=False)
    monkeypatch.delenv("BU_CDP_URL", raising=False)
    monkeypatch.delenv("BU_CDP_WS", raising=False)
    yield
    server._bh = original_bh
    server._bound_cdp = original_bound


def _patch_harness(helpers: MagicMock):
    return patch.object(server, "_browser_harness", return_value=(helpers, MagicMock()))


class _Helpers:
    name = "helpers"

    def js(self, expression: str) -> bool:
        return True

    def fill_input(self, selector: str, text: str, clear_first: bool = True) -> None:
        return None


def test_counting_proxy_counts_calls() -> None:
    inner = _Helpers()
    proxy = perf.CountingHelpers(inner)
    perf.reset_harness_calls()
    assert proxy.js("1+1") is True
    proxy.fill_input("input", "hello")
    assert perf.harness_call_count() == 2


def test_counting_proxy_hasattr_passthrough() -> None:
    proxy = perf.CountingHelpers(_Helpers())
    assert hasattr(proxy, "js")
    assert hasattr(proxy, "fill_input")
    assert hasattr(proxy, "name")
    assert not hasattr(proxy, "cdp")
    assert not hasattr(proxy, "drain_events")
    assert proxy.name == "helpers"


def test_wrap_helpers_is_identity_when_disabled() -> None:
    inner = _Helpers()
    assert perf.wrap_helpers(inner) is inner


def test_wrap_helpers_is_idempotent_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_MCP_PERF", "1")
    inner = _Helpers()
    wrapped = perf.wrap_helpers(inner)
    assert isinstance(wrapped, perf.CountingHelpers)
    assert perf.wrap_helpers(wrapped) is wrapped
    assert perf.unwrap(wrapped) is inner


def test_log_skipped_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "tools.jsonl"
    monkeypatch.setenv("BROWSER_MCP_PERF_LOG", str(log))
    helpers = MagicMock()
    helpers.page_info.return_value = {"url": "http://example.test", "title": "t"}
    with _patch_harness(helpers):
        server.browser_page_info()
    assert not log.exists()


def _read_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


_SCHEMA = {"ts", "tool", "wall_ms", "harness_calls", "result_chars", "ok"}


def test_log_line_has_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "tools.jsonl"
    monkeypatch.setenv("BROWSER_MCP_PERF", "1")
    monkeypatch.setenv("BROWSER_MCP_PERF_LOG", str(log))
    helpers = MagicMock()
    helpers.page_info.return_value = {"url": "http://example.test", "title": "t"}
    with _patch_harness(helpers):
        result = server.browser_page_info()
    records = _read_records(log)
    assert len(records) == 1
    rec = records[0]
    assert _SCHEMA <= set(rec)
    assert rec["tool"] == "browser_page_info"
    assert rec["ok"] is True
    assert rec["result_chars"] == len(result)
    assert rec["harness_calls"] == 0
    assert isinstance(rec["wall_ms"], float)
    assert isinstance(rec["ts"], str)


def test_arguments_never_appear_in_the_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "PERF_SECRET_TOKEN_XYZ_never_log"
    log = tmp_path / "tools.jsonl"
    monkeypatch.setenv("BROWSER_MCP_PERF", "1")
    monkeypatch.setenv("BROWSER_MCP_PERF_LOG", str(log))
    helpers = MagicMock()
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing,
            "get_input_info",
            return_value={"selector": '[data-bmcp-idx="12"]', "tagName": "input"},
        ),
    ):
        server.browser_input_by_index(12, secret)
    raw = log.read_text(encoding="utf-8")
    assert secret not in raw
    rec = _read_records(log)[0]
    assert rec["tool"] == "browser_input_by_index"
    assert set(rec) == _SCHEMA


def test_ok_false_when_tool_returns_error_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "tools.jsonl"
    monkeypatch.setenv("BROWSER_MCP_PERF", "1")
    monkeypatch.setenv("BROWSER_MCP_PERF_LOG", str(log))
    helpers = MagicMock()
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing,
            "get_rect",
            side_effect=dom_indexing.ElementNotFoundError("Element index not found."),
        ),
    ):
        result = json.loads(server.browser_click_by_index(99))
    assert result["ok"] is False
    rec = _read_records(log)[0]
    assert rec["ok"] is False
    assert rec["tool"] == "browser_click_by_index"


def test_ok_false_when_tool_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "tools.jsonl"
    monkeypatch.setenv("BROWSER_MCP_PERF", "1")
    monkeypatch.setenv("BROWSER_MCP_PERF_LOG", str(log))
    helpers = MagicMock()
    helpers.page_info.side_effect = RuntimeError("boom")
    with _patch_harness(helpers), pytest.raises(RuntimeError, match="boom"):
        server.browser_page_info()
    rec = _read_records(log)[0]
    assert rec["ok"] is False
    assert rec["tool"] == "browser_page_info"


def test_harness_calls_counted_through_wrapped_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from browser_mcp import tabs
    from browser_mcp.tabs import TabState

    log = tmp_path / "tools.jsonl"
    monkeypatch.setenv("BROWSER_MCP_PERF", "1")
    monkeypatch.setenv("BROWSER_MCP_PERF_LOG", str(log))
    inner = MagicMock()
    inner.page_info.return_value = {"url": "http://example.test", "title": "t"}
    wrapped = perf.wrap_helpers(inner)
    tabs.reset_registry()
    state = TabState(target_id="aaa", tab="t1", alias="t1")
    tabs.registry()._tabs["aaa"] = state
    tabs.registry()._aliases["t1"] = "aaa"
    tabs.registry().set_focused("aaa")
    with patch.object(server, "_browser_harness", return_value=(wrapped, MagicMock())):
        server.browser_page_info()
    rec = _read_records(log)[0]
    assert rec["tool"] == "browser_page_info"
    assert rec["harness_calls"] == 1


def test_browser_harness_wraps_helpers_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_MCP_PERF", "1")
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9333")
    admin = MagicMock()
    helpers = MagicMock()
    admin.daemon_alive.return_value = False
    mod = ModuleType("browser_harness")
    mod.admin = admin
    mod.helpers = helpers
    monkeypatch.setitem(sys.modules, "browser_harness", mod)

    wrapped, got_admin = server._browser_harness()

    assert isinstance(wrapped, perf.CountingHelpers)
    assert perf.unwrap(wrapped) is helpers
    assert got_admin is admin
    # Cached binding stays unwrapped so identity tests without perf stay green.
    assert server._bh == (helpers, admin)


def test_log_write_failure_does_not_fail_the_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BROWSER_MCP_PERF", "1")
    monkeypatch.setenv("BROWSER_MCP_PERF_LOG", str(tmp_path / "missing-dir-not-created-as-file"))
    # Point the log path at a directory so open-for-append fails.
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    monkeypatch.setenv("BROWSER_MCP_PERF_LOG", str(blocked))
    helpers = MagicMock()
    helpers.page_info.return_value = {"url": "http://example.test"}
    with _patch_harness(helpers):
        result = server.browser_page_info()
    assert "example.test" in result
