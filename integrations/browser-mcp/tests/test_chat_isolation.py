"""Per-chat daemons, registries, locks, and the 10-tab cap."""

from __future__ import annotations

import os
import threading
import time

import pytest
from browser_mcp import chat_context, in_app_cdp, tabs
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
