"""Tests for in-app chat scope announcement (``Monkeybot.setChatScope``)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
from browser_mcp import app, backend, chat_scope, in_app_cdp, tabs
from mcp.types import RequestParams


@pytest.fixture(autouse=True)
def _reset_chat_scope_state() -> None:
    original_bh = backend._bh
    original_bound = backend._bound_cdp
    original_flag = in_app_cdp._env_set_from_in_app_file
    chat_scope.reset()
    tabs.reset_registry()
    yield
    chat_scope.reset()
    tabs.reset_registry()
    backend._bh = original_bh
    backend._bound_cdp = original_bound
    in_app_cdp._env_set_from_in_app_file = original_flag


def _request_ctx(meta: object) -> SimpleNamespace:
    return SimpleNamespace(request_context=SimpleNamespace(meta=meta))


def _install_in_app_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> MagicMock:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("http://127.0.0.1:9333", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)
    in_app_cdp._env_set_from_in_app_file = False
    backend._bh = None
    backend._bound_cdp = None

    admin = MagicMock()
    helpers = MagicMock()
    admin.daemon_alive.return_value = False
    mod = ModuleType("browser_harness")
    mod.admin = admin
    mod.helpers = helpers
    monkeypatch.setitem(sys.modules, "browser_harness", mod)
    return helpers


def test_current_thread_id_from_meta_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    meta = RequestParams.Meta.model_validate(
        {"monkeybot": {"thread_id": "sess-1", "request_id": "r", "run_id": None}}
    )
    monkeypatch.setattr(app.mcp, "get_context", lambda: _request_ctx(meta))
    assert chat_scope.current_thread_id() == "sess-1"


def test_current_thread_id_absent_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.mcp, "get_context", lambda: _request_ctx(SimpleNamespace()))
    assert chat_scope.current_thread_id() is None


def test_current_thread_id_outside_request_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> None:
        raise LookupError("Context is not available outside of a request")

    monkeypatch.setattr(app.mcp, "get_context", _boom)
    assert chat_scope.current_thread_id() is None


def test_announce_sends_on_each_call_without_dropping_tabs() -> None:
    helpers = MagicMock()
    chat_scope.announce(helpers, "t-a")
    tabs.registry().ensure("target-a", url="https://a.example")
    chat_scope.announce(helpers, "t-a")
    assert helpers.cdp.call_count == 2
    assert tabs.registry().get("target-a") is not None

    chat_scope.announce(helpers, "t-b")
    assert helpers.cdp.call_count == 3
    helpers.cdp.assert_called_with("Monkeybot.setChatScope", chatKey="t-b")
    assert tabs.registry().get("target-a") is not None

    chat_scope.announce(helpers, None)
    helpers.cdp.assert_called_with("Monkeybot.setChatScope", chatKey=None)
    assert helpers.cdp.call_count == 4
    chat_scope.announce(helpers, None)
    assert helpers.cdp.call_count == 5


def test_announce_error_marks_unsupported_and_never_sends_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    helpers = MagicMock()
    helpers.cdp.side_effect = RuntimeError({"code": -32601, "message": "Method not found"})
    with caplog.at_level(logging.WARNING, logger="browser_mcp.chat_scope"):
        chat_scope.announce(helpers, "t-a")
        chat_scope.announce(helpers, "t-b")
    helpers.cdp.assert_called_once_with("Monkeybot.setChatScope", chatKey="t-a")
    assert any("unsupported" in r.message for r in caplog.records)


def test_announce_stringified_method_not_found_latches() -> None:
    helpers = MagicMock()
    helpers.cdp.side_effect = RuntimeError(
        "{'code': -32601, 'message': \"'Monkeybot.setChatScope' wasn't found\"}"
    )
    chat_scope.announce(helpers, "t-a")
    helpers.cdp.reset_mock()
    chat_scope.announce(helpers, "t-b")
    helpers.cdp.assert_not_called()


def test_announce_transport_error_does_not_latch_and_retries() -> None:
    helpers = MagicMock()
    helpers.cdp.side_effect = RuntimeError("timed out")
    chat_scope.announce(helpers, "t-a")
    helpers.cdp.assert_called_once_with("Monkeybot.setChatScope", chatKey="t-a")

    helpers.cdp.side_effect = None
    helpers.cdp.reset_mock()
    chat_scope.announce(helpers, "t-a")
    helpers.cdp.assert_called_once_with("Monkeybot.setChatScope", chatKey="t-a")


def test_announce_keeps_tabs_when_chat_switches() -> None:
    helpers = MagicMock()
    chat_scope.announce(helpers, "t-a")
    tabs.registry().ensure("target-a", url="https://a.example")
    assert tabs.registry().get("target-a") is not None

    chat_scope.announce(helpers, "t-b")
    assert tabs.registry().get("target-a") is not None


def test_announce_keeps_tabs_when_chat_is_unchanged() -> None:
    helpers = MagicMock()
    chat_scope.announce(helpers, "t-a")
    tabs.registry().ensure("target-a", url="https://a.example")
    chat_scope.announce(helpers, "t-a")
    assert tabs.registry().get("target-a") is not None


def test_announce_keeps_tabs_across_none_then_same_chat() -> None:
    helpers = MagicMock()
    chat_scope.announce(helpers, "t-a")
    tabs.registry().ensure("target-a", url="https://a.example")
    chat_scope.announce(helpers, None)
    chat_scope.announce(helpers, "t-a")
    assert tabs.registry().get("target-a") is not None


def test_announce_keeps_tabs_on_switch_even_when_unsupported() -> None:
    helpers = MagicMock()
    helpers.cdp.side_effect = RuntimeError("unknown method")
    chat_scope.announce(helpers, "t-a")
    tabs.registry().ensure("target-a", url="https://a.example")

    chat_scope.announce(helpers, "t-b")
    assert tabs.registry().get("target-a") is not None


def test_announce_never_raises_without_cdp() -> None:
    chat_scope.announce(object(), "t-a")
    chat_scope.announce(None, "t-a")


def test_reset_re_enables_after_unsupported() -> None:
    helpers = MagicMock()
    helpers.cdp.side_effect = RuntimeError("unknown method: Monkeybot.setChatScope")
    chat_scope.announce(helpers, "t-a")
    helpers.cdp.assert_called_once()

    chat_scope.reset()
    helpers.cdp.side_effect = None
    helpers.cdp.reset_mock()
    chat_scope.announce(helpers, "t-a")
    helpers.cdp.assert_called_once_with("Monkeybot.setChatScope", chatKey="t-a")


def test_public_tool_skips_announce_when_not_in_app() -> None:
    helpers = MagicMock()
    backend._bh = (helpers, MagicMock())
    backend._bound_cdp = "http://127.0.0.1:9222"
    in_app_cdp._env_set_from_in_app_file = False

    @app._public_tool
    def ping() -> str:
        return "ok"

    assert ping() == "ok"
    helpers.cdp.assert_not_called()


def test_public_tool_skips_announce_for_agentcore() -> None:
    helpers = MagicMock()
    backend._bh = (helpers, MagicMock())
    backend._bound_cdp = "agentcore"
    in_app_cdp._env_set_from_in_app_file = False

    @app._public_tool
    def ping() -> str:
        return "ok"

    assert ping() == "ok"
    helpers.cdp.assert_not_called()


def test_public_tool_keeps_each_chat_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from browser_mcp import chat_context

    _install_in_app_harness(monkeypatch, tmp_path)
    monkeypatch.delenv("MONKEYBOT_RUN_ID", raising=False)
    seen: list[str] = []

    def thread_id() -> str:
        return seen[-1]

    monkeypatch.setattr(chat_scope, "current_thread_id", thread_id)

    @app._public_tool
    def browse() -> str:
        backend.browser_harness()
        tabs.registry().ensure(f"target-{seen[-1]}", url="https://example.test")
        return "ok"

    seen.append("chat-a")
    assert browse() == "ok"
    seen.append("chat-a")
    assert browse() == "ok"
    seen.append("chat-b")
    assert browse() == "ok"

    chat_a = chat_context.context_for_call("chat-a")
    chat_b = chat_context.context_for_call("chat-b")
    assert chat_a is not chat_b
    assert chat_a.daemon_name != chat_b.daemon_name
    assert chat_a.registry.get("target-chat-a") is not None
    assert chat_a.registry.get("target-chat-b") is None
    assert chat_b.registry.get("target-chat-b") is not None
    assert chat_b.registry.get("target-chat-a") is None


def test_public_tool_continues_when_announce_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helpers = _install_in_app_harness(monkeypatch, tmp_path)
    helpers.cdp.side_effect = RuntimeError({"code": -32601, "message": "Method not found"})

    @app._public_tool
    def browse() -> str:
        backend.browser_harness()
        return "ok"

    assert browse() == "ok"


def test_in_app_bind_resets_and_announces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helpers = _install_in_app_harness(monkeypatch, tmp_path)
    monkeypatch.setattr(chat_scope, "current_thread_id", lambda: "sess-bind")

    backend.browser_harness()
    helpers.cdp.assert_called_once_with("Monkeybot.setChatScope", chatKey="sess-bind")
    assert backend.in_app_backend_active()

    helpers.cdp.reset_mock()
    backend.browser_harness()
    helpers.cdp.assert_not_called()


def test_in_app_endpoint_change_resets_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helpers = MagicMock()
    helpers.cdp.side_effect = RuntimeError("unknown method: Monkeybot.setChatScope")
    chat_scope.announce(helpers, "t-a")
    assert helpers.cdp.call_count == 1

    monkeypatch.setenv("BU_CDP_WS", "ws://127.0.0.1:9333/devtools/browser/monkeybot")
    in_app_cdp._bind_in_app_endpoint("http://127.0.0.1:9444", "tok")
    helpers.cdp.side_effect = None
    helpers.cdp.reset_mock()
    chat_scope.announce(helpers, "t-a")
    helpers.cdp.assert_called_once_with("Monkeybot.setChatScope", chatKey="t-a")
