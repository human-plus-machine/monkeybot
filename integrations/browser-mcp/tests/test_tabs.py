"""Unit tests for the Phase 2 tab registry, focus rules, and cap."""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from browser_mcp import dom_indexing, server, tabs, backend, tab_ops
from browser_mcp.tabs import TabRegistry, TabState, UnknownTabError, runtime_value


@pytest.fixture(autouse=True)
def _reset() -> None:
    original = backend._bh
    original_bound = backend._bound_cdp
    backend._bh = None
    backend._bound_cdp = None
    dom_indexing.clear_registered_targets()
    yield
    backend._bh = original
    backend._bound_cdp = original_bound
    dom_indexing.clear_registered_targets()


def _patch_harness(helpers: MagicMock):
    return patch.object(backend, "browser_harness", return_value=(helpers, MagicMock()))


def _tab(tid: str, url: str, title: str = "t") -> dict:
    return {"targetId": tid, "target_id": tid, "url": url, "title": title}


def _helpers(*, tabs_list: list[dict], focused: str, **extra: object) -> MagicMock:
    helpers = MagicMock()
    helpers.list_tabs.return_value = list(tabs_list)
    focused_row = next(t for t in tabs_list if t["targetId"] == focused)
    helpers.current_tab.return_value = dict(focused_row)
    helpers.page_info.return_value = {
        "url": focused_row["url"],
        "title": focused_row["title"],
        "w": 800,
        "h": 600,
    }
    helpers.js.return_value = True
    helpers.switch_tab.return_value = "sid"
    for name, value in extra.items():
        setattr(helpers, name, value)
    return helpers


def test_refresh_assigns_and_retires_aliases() -> None:
    reg = TabRegistry()
    helpers = MagicMock()
    helpers.list_tabs.return_value = [
        _tab("aaa", "https://a.test/"),
        _tab("bbb", "https://b.test/"),
    ]
    helpers.current_tab.return_value = _tab("aaa", "https://a.test/")
    reg.refresh(helpers)
    assert {s.tab for s in reg.tabs()} == {"t1", "t2"}
    assert reg.resolve("t1").target_id == "aaa"
    assert reg.resolve("aaa").tab == "t1"

    helpers.list_tabs.return_value = [_tab("bbb", "https://b.test/"), _tab("ccc", "https://c.test/")]
    helpers.current_tab.return_value = _tab("bbb", "https://b.test/")
    reg.refresh(helpers)
    aliases = {s.tab for s in reg.tabs()}
    assert "t1" not in aliases
    assert "t2" in aliases
    assert "t3" in aliases
    with pytest.raises(UnknownTabError, match="unknown tab 't1'"):
        reg.resolve("t1")
    # Retired aliases are never reused.
    assert reg.resolve("t3").target_id == "ccc"


def test_resolve_accepts_alias_and_raw_id() -> None:
    reg = TabRegistry()
    helpers = MagicMock()
    helpers.list_tabs.return_value = [_tab("deadbeef", "https://a.test/", "A")]
    helpers.current_tab.return_value = _tab("deadbeef", "https://a.test/", "A")
    reg.refresh(helpers)
    state = reg.resolve("t1")
    assert state is reg.resolve("deadbeef")
    with pytest.raises(UnknownTabError, match="known: t1 \\(focused\\)"):
        reg.resolve("foo")


def test_set_alias_unique_and_pattern() -> None:
    reg = TabRegistry()
    helpers = MagicMock()
    helpers.list_tabs.return_value = [
        _tab("a", "https://a.test/"),
        _tab("b", "https://b.test/"),
    ]
    helpers.current_tab.return_value = _tab("a", "https://a.test/")
    reg.refresh(helpers)
    reg.set_alias("t1", "search")
    assert reg.resolve("search").target_id == "a"
    with pytest.raises(ValueError, match="already used"):
        reg.set_alias("t2", "search")
    with pytest.raises(ValueError, match="must match"):
        reg.set_alias("t2", "NOPE")


def test_sixth_agent_tab_returns_cap_without_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROWSER_MCP_MAX_TABS", "5")
    rows = [_tab(f"id{i}", f"https://ex.test/{i}") for i in range(5)]
    helpers = _helpers(tabs_list=rows, focused="id0")
    helpers.cdp = MagicMock()
    with _patch_harness(helpers):
        json.loads(server.browser_tabs())
        reg = tabs.registry()
        for state in reg.tabs():
            state.touched_by_agent = True
        result = json.loads(server.browser_open_tab("https://ex.test/new"))
    assert result["ok"] is False
    assert result["error"] == "tab_limit_reached"
    assert result["limit"] == 5
    assert len(result["tabs"]) == 5
    assert "Ask the user" in result["action_required"]
    helpers.cdp.assert_not_called()


def test_user_opened_untouched_tabs_do_not_count_toward_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROWSER_MCP_MAX_TABS", "5")
    rows = [_tab(f"id{i}", f"https://ex.test/{i}") for i in range(5)]
    helpers = _helpers(tabs_list=rows, focused="id0")
    helpers.cdp = MagicMock(return_value={"targetId": "id-new", "sessionId": "s"})
    helpers.list_tabs.side_effect = [
        rows,
        rows,
        rows + [_tab("id-new", "about:blank")],
        rows + [_tab("id-new", "https://ex.test/new")],
    ]
    with _patch_harness(helpers):
        json.loads(server.browser_tabs())
        result = json.loads(server.browser_open_tab("https://ex.test/new", focus=False))
    assert result["ok"] is True
    helpers.cdp.assert_any_call(
        "Target.createTarget", url="about:blank", background=True
    )


def test_close_tab_then_retry_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_MCP_MAX_TABS", "1")
    user = _tab("user", "https://user.test/")
    agent = _tab("agent", "https://agent.test/")
    created = _tab("new", "https://new.test/")
    helpers = _helpers(tabs_list=[user, agent], focused="agent")
    def cdp(method: str, **_params: object) -> dict:
        if method == "Target.createTarget":
            return {"targetId": "new"}
        if method == "Target.attachToTarget":
            return {"sessionId": "sid"}
        return {"result": {"type": "string", "value": True}}

    helpers.cdp.side_effect = cdp
    helpers.close_tab = MagicMock()
    with _patch_harness(helpers):
        json.loads(server.browser_tabs())
        tabs.registry().resolve("t2").touched_by_agent = True
        blocked = json.loads(server.browser_open_tab("https://new.test/"))
        assert blocked["error"] == "tab_limit_reached"

        def list_tabs(*_a, **_k):
            if any(
                c.args and c.args[0] == "Target.createTarget"
                for c in helpers.cdp.call_args_list
            ):
                return [user, created]
            if helpers.close_tab.called:
                return [user]
            return [user, agent]

        helpers.list_tabs.side_effect = list_tabs
        helpers.current_tab.return_value = user
        closed = json.loads(server.browser_close_tab("t2"))
        assert closed["ok"] is True
        retry = json.loads(server.browser_open_tab("https://new.test/", focus=False))
    assert retry["ok"] is True
    helpers.close_tab.assert_called_once_with("agent")


def test_browser_stop_closes_agent_opened_tabs_at_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROWSER_MCP_MAX_TABS", "5")
    rows = [_tab(f"id{i}", f"https://ex.test/{i}") for i in range(5)]
    helpers = _helpers(tabs_list=rows, focused="id0")
    helpers.close_tab = MagicMock()
    backend._bh = (helpers, MagicMock())
    backend._bound_cdp = "http://127.0.0.1:9222"
    reg = tabs.registry()
    reg.refresh(helpers)
    for state in reg.tabs():
        state.opened_by_agent = True
    with patch("browser_harness.admin.restart_daemon"):
        result = json.loads(server.browser_stop())
    assert result["ok"] is True
    assert helpers.close_tab.call_count == 5


def test_session_for_attaches_once_and_reattaches_on_lost_session() -> None:
    helpers = MagicMock()
    helpers.cdp.side_effect = [
        {"sessionId": "sid-1"},
        {},
        {},
        RuntimeError("Session with given id not found"),
        {"sessionId": "sid-2"},
        {},
        {},
        {"result": {"type": "string", "value": "ok"}},
    ]
    state = TabState(target_id="aaa", tab="t1", alias="t1")
    tabs.reset_registry()
    tabs.registry()._tabs["aaa"] = state
    tabs.registry()._aliases["t1"] = "aaa"
    handle = tabs.TabHandle(helpers, state, focused=False)
    assert handle.evaluate("1+1") == "ok"
    methods = [c.args[0] for c in helpers.cdp.call_args_list]
    assert methods.count("Target.attachToTarget") == 2
    assert state.session_id == "sid-2"


def test_for_action_switches_only_when_target_differs() -> None:
    rows = [_tab("aaa", "https://a.test/"), _tab("bbb", "https://b.test/")]
    helpers = _helpers(tabs_list=rows, focused="aaa")
    with _patch_harness(helpers):
        json.loads(server.browser_tabs())
        tab_ops._for_action(helpers, "t1")
        helpers.switch_tab.assert_not_called()
        tab_ops._for_action(helpers, "t2")
        helpers.switch_tab.assert_called_once_with("bbb")


def test_for_read_never_calls_switch_tab() -> None:
    rows = [_tab("aaa", "https://a.test/"), _tab("bbb", "https://b.test/")]
    helpers = _helpers(tabs_list=rows, focused="aaa")
    helpers.js.return_value = {"tree": "[0]<a>x</a>", "elementCount": 1}
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing,
            "get_elements",
            return_value={"tree": "[0]<a>x</a>", "elementCount": 1},
        ),
    ):
        json.loads(server.browser_tabs())
        json.loads(server.browser_get_elements(tab="t2"))
    helpers.switch_tab.assert_not_called()


def test_wait_idle_on_background_tab_returns_settle_note() -> None:
    rows = [_tab("aaa", "https://a.test/"), _tab("bbb", "https://b.test/")]
    helpers = _helpers(tabs_list=rows, focused="aaa")
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "settle", return_value={"quiet": True}),
    ):
        json.loads(server.browser_tabs())
        result = json.loads(server.browser_wait_idle(tab="t2"))
    assert result["ok"] is True
    assert result["idle"] is None
    assert "network idle is only available on the focused tab" in result["note"]
    helpers.wait_for_network_idle.assert_not_called()


def test_rlock_serializes_concurrent_tool_calls() -> None:
    order: list[str] = []
    helpers = MagicMock()
    started = threading.Event()
    release = threading.Event()

    def click(*_a, **_k):
        order.append("enter")
        started.set()
        assert release.wait(timeout=2)
        order.append("leave")

    helpers.click_at_xy.side_effect = click
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "get_rect", return_value={"x": 1, "y": 2}),
    ):
        t1 = threading.Thread(target=lambda: server.browser_click_by_index(1, observe="none"))
        t2 = threading.Thread(target=lambda: server.browser_click_by_index(2, observe="none"))
        t1.start()
        assert started.wait(timeout=2)
        t2.start()
        time.sleep(0.05)
        assert order == ["enter"]
        release.set()
        t1.join(timeout=2)
        t2.join(timeout=2)
    assert order == ["enter", "leave", "enter", "leave"]


def test_browser_tabs_shape() -> None:
    rows = [_tab("aaa", "https://a.test/", "A"), _tab("bbb", "https://b.test/", "B")]
    helpers = _helpers(tabs_list=rows, focused="bbb")
    with _patch_harness(helpers):
        result = json.loads(server.browser_tabs())
    assert result["ok"] is True
    assert result["focused"] == "t2"
    assert result["tabs"][0]["focused"] is True
    assert result["tabs"][0]["tab"] == "t2"
    assert "opened_by_agent" in result["tabs"][0]
    assert "last_used" in result["tabs"][0]


def test_unknown_tab_error_payload() -> None:
    helpers = _helpers(tabs_list=[_tab("aaa", "https://a.test/")], focused="aaa")
    with _patch_harness(helpers):
        json.loads(server.browser_tabs())
        result = json.loads(server.browser_get_elements(tab="nope"))
    assert result["ok"] is False
    assert "unknown tab 'nope'" in result["error"]


def test_runtime_value_decodes_and_raises() -> None:
    assert runtime_value({"result": {"type": "number", "value": 3}}, "1+2") == 3
    with pytest.raises(RuntimeError, match="JavaScript evaluation failed"):
        runtime_value(
            {"result": {"subtype": "error", "description": "boom"}, "exceptionDetails": {}},
            "throw",
        )


def test_playwright_page_map_background_evaluate() -> None:
    helpers = MagicMock(spec=["js", "list_tabs", "current_tab", "goto_url"])
    helpers.js.return_value = "from-bg"
    helpers.list_tabs.return_value = [
        _tab("111", "https://a.test/"),
        _tab("222", "https://b.test/"),
    ]
    helpers.current_tab.return_value = _tab("111", "https://a.test/")
    reg = TabRegistry()
    reg.refresh(helpers)
    state = reg.resolve("t2")
    handle = reg.handle(helpers, state, focused=False)
    assert handle.evaluate("document.title") == "from-bg"
    helpers.js.assert_called_with("document.title", target_id="222")
    helpers.goto_url.assert_not_called()
