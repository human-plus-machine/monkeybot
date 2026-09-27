"""Per-chat daemons, registries, locks, and the 10-tab cap."""

from __future__ import annotations

import inspect
import os
import threading
import time

import pytest
from browser_mcp import app, chat_context, chat_scope, in_app_cdp, tab_ops, tabs
from browser_mcp.tabs import TabLimitError


def test_two_threads_get_distinct_daemons_and_registries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MONKEYBOT_RUN_ID", "run-1")
    monkeypatch.setenv("BU_NAME", "monkeybot")
    monkeypatch.setattr(in_app_cdp, "_env_set_from_in_app_file", True)

    first = chat_context.context_for_call("chat-a")
    second = chat_context.context_for_call("chat-b")
    assert first is not second
    assert first.daemon_name != second.daemon_name
    assert first.daemon_name is not None
    assert first.daemon_name.startswith("monkeybot-")
    assert len(first.daemon_name) <= 64
    first.registry.ensure("only-a")
    assert second.registry.get("only-a") is None
    assert chat_context.context_for_call("chat-a") is first


def test_chat_switch_does_not_reset_the_other_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(in_app_cdp, "_env_set_from_in_app_file", True)
    monkeypatch.delenv("MONKEYBOT_RUN_ID", raising=False)
    chat_a = chat_context.context_for_call("chat-a")
    chat_a.registry.ensure("target-a", url="https://a.example")
    chat_b = chat_context.context_for_call("chat-b")
    assert chat_a.registry.get("target-a") is not None
    assert chat_b.registry.get("target-a") is None


def test_default_tab_cap_is_ten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROWSER_MCP_MAX_TABS", raising=False)
    assert tabs.max_tabs() == 10


def test_eleventh_open_is_tab_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROWSER_MCP_MAX_TABS", raising=False)
    reg = tabs.TabRegistry()
    for index in range(10):
        state = reg.ensure(f"t-{index}")
        state.opened_by_agent = True
    assert reg.would_exceed_cap()
    payload = reg.cap_error_payload()
    assert payload["error"] == "tab_limit_reached"
    assert payload["limit"] == 10


def test_app_tab_limit_error_uses_the_same_payload() -> None:
    reg = tabs.registry()
    err = TabLimitError(reg.cap_error_payload())
    assert err.payload["error"] == "tab_limit_reached"


def test_daemon_url_carries_chat(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text(
        "ws://127.0.0.1:9333/devtools/browser/monkeybot", encoding="utf-8"
    )
    (tmp_path / "in-app-cdp-token").write_text("secret-token", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)
    monkeypatch.setenv("MONKEYBOT_RUN_ID", "run-123")
    monkeypatch.setattr(in_app_cdp, "_env_set_from_in_app_file", False)

    bound = in_app_cdp.daemon_cdp_endpoint("chat-a")
    assert bound is not None
    assert "run=run-123" in bound
    assert "chat=chat-a" in bound
    assert "token=secret-token" in bound
    # The process-wide URL must not pick up this chat, or the other chat's
    # daemon would be restarted onto it.
    assert "chat=" not in (os.environ.get("BU_CDP_WS") or "")


def test_chats_do_not_block_each_other(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(in_app_cdp, "_env_set_from_in_app_file", True)
    monkeypatch.delenv("MONKEYBOT_RUN_ID", raising=False)
    holding = threading.Event()
    order: list[str] = []

    def hold(name: str) -> None:
        ctx = chat_context.context_for_call(name)
        tokens = chat_context.enter(ctx)
        try:
            with ctx.lock:
                if name == "chat-a":
                    holding.set()
                    time.sleep(0.25)
                    order.append("chat-a-out")
                else:
                    assert holding.wait(timeout=2)
                    order.append("chat-b-out")
        finally:
            chat_context.leave(tokens)

    slow = threading.Thread(target=hold, args=("chat-a",))
    fast = threading.Thread(target=hold, args=("chat-b",))
    slow.start()
    fast.start()
    slow.join(timeout=3)
    fast.join(timeout=3)
    assert not slow.is_alive()
    assert not fast.is_alive()
    # chat-b takes its own lock and finishes while chat-a still holds its lock.
    assert order.index("chat-b-out") < order.index("chat-a-out")


def test_run_headers_include_chat_when_context_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MONKEYBOT_RUN_ID", "run-9")
    monkeypatch.delenv("MONKEYBOT_RUN_LABEL", raising=False)
    monkeypatch.setattr(in_app_cdp, "_env_set_from_in_app_file", True)
    ctx = chat_context.context_for_call("thread-9")
    tokens = chat_context.enter(ctx)
    try:
        assert in_app_cdp._run_headers()["X-Monkeybot-Chat"] == "thread-9"
        assert in_app_cdp._run_headers()["X-Monkeybot-Run"] == "run-9"
    finally:
        chat_context.leave(tokens)


def test_send_router_targets_the_chat_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    import browser_harness.helpers as helpers

    seen: dict[str, object] = {}
    monkeypatch.setattr(in_app_cdp, "_env_set_from_in_app_file", True)
    monkeypatch.setenv("MONKEYBOT_RUN_ID", "route-run")
    monkeypatch.setattr(chat_context, "_router_installed", False)
    monkeypatch.setattr(helpers, "_monkeybot_send_routed", False, raising=False)

    def connect(name: str, timeout: float = 5.0):
        seen["name"] = name
        return type("Conn", (), {"close": lambda self: None})(), "tok"

    def request(connection: object, token: object, req: object):
        seen["req"] = req
        return {"result": {}}

    monkeypatch.setattr(chat_context._harness_ipc, "connect", connect)
    monkeypatch.setattr(chat_context._harness_ipc, "request", request)
    chat_context.install_send_router()
    ctx = chat_context.context_for_call("route-chat")
    tokens = chat_context.enter(ctx)
    try:
        helpers._send({"method": "Target.getTargets"})
    finally:
        chat_context.leave(tokens)
    assert seen["name"] == ctx.daemon_name
    assert seen["name"] != helpers.NAME


def test_context_lookup_is_not_on_the_tool_hot_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(chat_context, "_sweep_idle", lambda now=None: calls.append("sweep"))
    chat_context.context_for_call(None)
    assert calls == []


def test_reset_clears_only_the_active_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(in_app_cdp, "_env_set_from_in_app_file", True)
    monkeypatch.delenv("MONKEYBOT_RUN_ID", raising=False)
    first = chat_context.context_for_call("reset-a")
    second = chat_context.context_for_call("reset-b")
    first.announced = True
    second.announced = True
    tokens = chat_context.enter(first)
    try:
        chat_scope.reset()
    finally:
        chat_context.leave(tokens)
    assert first.announced is False
    assert second.announced is True


def test_unsupported_latch_does_not_cross_chats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(in_app_cdp, "_env_set_from_in_app_file", True)
    monkeypatch.delenv("MONKEYBOT_RUN_ID", raising=False)
    chat_a = chat_context.context_for_call("unsup-a")
    chat_b = chat_context.context_for_call("unsup-b")
    calls: list[str] = []

    class Bad:
        def cdp(self, method: str, **kwargs: object) -> None:
            raise RuntimeError("unknown method")

    class Good:
        def cdp(self, method: str, **kwargs: object) -> None:
            calls.append(method)

    tokens = chat_context.enter(chat_a)
    try:
        chat_scope.announce(Bad(), "unsup-a")
    finally:
        chat_context.leave(tokens)
    tokens = chat_context.enter(chat_b)
    try:
        chat_scope.announce(Good(), "unsup-b")
    finally:
        chat_context.leave(tokens)
    assert calls == ["Monkeybot.setChatScope"]


def test_idle_sweep_stops_daemon_and_keeps_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(in_app_cdp, "_env_set_from_in_app_file", True)
    monkeypatch.setenv("MONKEYBOT_RUN_ID", "idle-run")
    stopped: list[str] = []
    monkeypatch.setattr(chat_context, "_stop_daemon", lambda ctx: stopped.append(ctx.daemon_name or ""))
    ctx = chat_context.context_for_call("idle-chat")
    ctx.registry.ensure("target-kept", url="https://kept.example")
    ctx.bh = ("helpers", "admin")
    ctx.last_used = time.monotonic() - (chat_context.IDLE_SECONDS + 5)
    chat_context._sweep_idle()
    again = chat_context.context_for_call("idle-chat")
    assert again is ctx
    assert again.registry.get("target-kept") is not None
    assert again.bh is None
    assert stopped == [ctx.daemon_name]


def test_drop_keeps_the_in_flight_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(in_app_cdp, "_env_set_from_in_app_file", True)
    monkeypatch.delenv("MONKEYBOT_RUN_ID", raising=False)
    monkeypatch.setattr(chat_context, "_stop_daemon", lambda ctx: None)
    ctx = chat_context.context_for_call("lock-chat")
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        tokens = chat_context.enter(ctx)
        try:
            with ctx.lock:
                chat_context.drop(ctx)
                entered.set()
                release.wait(timeout=2)
        finally:
            chat_context.leave(tokens)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(timeout=2)
    other = chat_context.context_for_call("lock-chat")
    acquired = other.lock.acquire(blocking=False)
    if acquired:
        other.lock.release()
    release.set()
    thread.join(timeout=2)
    assert other is ctx
    assert acquired is False


def test_app_tab_limit_uses_code_and_scope() -> None:
    assert tab_ops._is_app_tab_limit(RuntimeError("{'code': -32010, 'message': 'too many live tabs'}"))
    assert tab_ops._is_app_tab_limit(RuntimeError("{'code': -32000, 'message': 'Tab limit reached (12)'}"))
    assert not tab_ops._is_app_tab_limit(RuntimeError("the user wrote tab limit reached in the page text"))
    reg = tabs.TabRegistry()
    reg.ensure("only-one")
    payload = tab_ops._app_tab_limit_payload(reg)
    assert payload["scope"] == "app"
    assert "limit" not in payload
    assert reg.cap_error_payload()["scope"] == "chat"


def test_registered_tools_run_async() -> None:
    from browser_mcp.server import mcp

    tools = mcp._tool_manager.list_tools()
    assert tools
    assert all(tool.is_async and inspect.iscoroutinefunction(tool.fn) for tool in tools)


def test_async_entry_looks_up_context_off_the_event_loop() -> None:
    import anyio

    seen: dict[str, int] = {}
    original = chat_context.context_for_call

    def wrapped(thread_id: str | None):
        seen["lookup"] = threading.get_ident()
        return original(None)

    @app._public_tool
    def probe() -> str:
        return "ok"

    async def main() -> None:
        seen["loop"] = threading.get_ident()
        chat_context.context_for_call = wrapped  # type: ignore[method-assign]
        try:
            await probe._async_entry()
        finally:
            chat_context.context_for_call = original  # type: ignore[method-assign]

    anyio.run(main)
    assert seen["lookup"] != seen["loop"]
