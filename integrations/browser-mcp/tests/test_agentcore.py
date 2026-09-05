"""AgentCore backend tests: dispatch, session lifecycle, teardown on stop/shutdown.

No real AWS/browser calls -- browser_session, AgentCoreAdmin, and
playwright_helpers.connect/disconnect are all mocked.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
from browser_mcp import agentcore, playwright_helpers, server  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    original_bh = server._bh
    original_bound = server._bound_cdp
    original_admin = server._agentcore_admin
    original_from_file = server._env_set_from_in_app_file
    server._bh = None
    server._bound_cdp = None
    server._agentcore_admin = None
    server._env_set_from_in_app_file = False
    monkeypatch.delenv("BROWSER_BACKEND", raising=False)
    monkeypatch.delenv("BU_CDP_URL", raising=False)
    monkeypatch.delenv("BU_CDP_WS", raising=False)
    monkeypatch.setattr(server, "_read_in_app_cdp_file", lambda: None)
    monkeypatch.setattr(server, "_read_in_app_cdp_token", lambda: None)
    yield
    server._bh = original_bh
    server._bound_cdp = original_bound
    server._agentcore_admin = original_admin
    server._env_set_from_in_app_file = original_from_file


# --- agentcore_backend_requested() ---


def test_agentcore_not_requested_when_backend_unset() -> None:
    assert agentcore.agentcore_backend_requested() is False


def test_agentcore_requested_when_backend_set_and_no_cdp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_BACKEND", "agentcore")
    assert agentcore.agentcore_backend_requested() is True


def test_agentcore_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_BACKEND", "AgentCore")
    assert agentcore.agentcore_backend_requested() is True


def test_explicit_cdp_ws_wins_over_agentcore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_BACKEND", "agentcore")
    monkeypatch.setenv("BU_CDP_WS", "ws://127.0.0.1:9222/devtools")
    assert agentcore.agentcore_backend_requested() is False


def test_explicit_cdp_url_wins_over_agentcore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_BACKEND", "agentcore")
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9222")
    assert agentcore.agentcore_backend_requested() is False


def test_other_backend_value_not_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_BACKEND", "browser-use-cloud")
    assert agentcore.agentcore_backend_requested() is False


# --- resolve_region() ---


def test_resolve_region_defaults_to_us_east_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert agentcore.resolve_region() == "us-east-1"


def test_resolve_region_prefers_aws_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    assert agentcore.resolve_region() == "eu-west-1"


def test_resolve_region_falls_back_to_aws_default_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    assert agentcore.resolve_region() == "us-west-2"


# --- AgentCoreAdmin session lifecycle ---


def _fake_browser_session_module():
    client = MagicMock()
    client.generate_ws_headers.return_value = ("wss://example/ws", {"Authorization": "sig"})
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=client)
    cm.__exit__ = MagicMock(return_value=False)
    session_factory = MagicMock(return_value=cm)
    return session_factory, cm, client


def _patched_browser_client(monkeypatch: pytest.MonkeyPatch, session_factory) -> None:
    """Inject a fake bedrock_agentcore.tools.browser_client module tree.

    bedrock-agentcore is an optional (agentcore-extra) dependency, not
    installed in the base test environment, so patch() can't resolve its
    dotted path -- fake the module hierarchy in sys.modules instead.
    """
    tools_mod = ModuleType("bedrock_agentcore.tools")
    browser_client_mod = ModuleType("bedrock_agentcore.tools.browser_client")
    browser_client_mod.browser_session = session_factory
    top_mod = ModuleType("bedrock_agentcore")
    top_mod.tools = tools_mod
    monkeypatch.setitem(sys.modules, "bedrock_agentcore", top_mod)
    monkeypatch.setitem(sys.modules, "bedrock_agentcore.tools", tools_mod)
    monkeypatch.setitem(sys.modules, "bedrock_agentcore.tools.browser_client", browser_client_mod)


def test_ensure_session_starts_once_and_returns_ws_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory, cm, client = _fake_browser_session_module()
    _patched_browser_client(monkeypatch, session_factory)
    admin = agentcore.AgentCoreAdmin("us-east-1")

    result1 = admin.ensure_session()
    result2 = admin.ensure_session()

    assert result1 == ("wss://example/ws", {"Authorization": "sig"})
    assert result2 == result1
    session_factory.assert_called_once_with("us-east-1", identifier=agentcore.DEFAULT_IDENTIFIER)
    cm.__enter__.assert_called_once()
    assert client.generate_ws_headers.call_count == 2


def test_ensure_session_uses_configured_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTCORE_BROWSER_ID", "custom.browser.v2")
    session_factory, _cm, _client = _fake_browser_session_module()
    _patched_browser_client(monkeypatch, session_factory)
    admin = agentcore.AgentCoreAdmin("us-east-1")

    admin.ensure_session()

    session_factory.assert_called_once_with("us-east-1", identifier="custom.browser.v2")


def test_stop_session_calls_exit_once(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory, cm, _client = _fake_browser_session_module()
    _patched_browser_client(monkeypatch, session_factory)
    admin = agentcore.AgentCoreAdmin("us-east-1")

    admin.ensure_session()
    admin.stop_session()

    cm.__exit__.assert_called_once_with(None, None, None)


def test_stop_session_idempotent_before_and_after_start(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = agentcore.AgentCoreAdmin("us-east-1")
    admin.stop_session()  # never started -- must not raise

    session_factory, cm, _client = _fake_browser_session_module()
    _patched_browser_client(monkeypatch, session_factory)
    admin.ensure_session()
    admin.stop_session()
    admin.stop_session()  # second call is a no-op

    cm.__exit__.assert_called_once()


# --- server._browser_harness() dispatch ---


def test_browser_harness_dispatches_to_agentcore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_BACKEND", "agentcore")
    fake_admin = MagicMock()
    fake_admin.ensure_session.return_value = ("wss://example/ws", {"Authorization": "sig"})
    fake_playwright_helpers = MagicMock()

    with (
        patch("browser_mcp.agentcore.AgentCoreAdmin", return_value=fake_admin),
        patch("browser_mcp.playwright_helpers", fake_playwright_helpers),
    ):
        helpers, admin = server._browser_harness()

    assert helpers is fake_playwright_helpers
    assert admin is fake_admin
    fake_admin.ensure_session.assert_called_once()
    fake_playwright_helpers.connect.assert_called_once_with("wss://example/ws", {"Authorization": "sig"})
    assert server._bound_cdp == "agentcore"


def test_browser_harness_reuses_bound_agentcore_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_BACKEND", "agentcore")
    fake_helpers = MagicMock()
    fake_admin = MagicMock()
    server._bh = (fake_helpers, fake_admin)
    server._bound_cdp = "agentcore"

    helpers, admin = server._browser_harness()

    assert (helpers, admin) == (fake_helpers, fake_admin)
    fake_admin.ensure_session.assert_not_called()


def test_explicit_cdp_still_wins_with_backend_agentcore_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """BROWSER_BACKEND=agentcore is set, but an explicit BU_CDP_URL must still
    route through the existing browser_harness path, not AgentCore."""
    monkeypatch.setenv("BROWSER_BACKEND", "agentcore")
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9222")

    admin = MagicMock()
    helpers = MagicMock()
    admin.daemon_alive.return_value = False
    mod = ModuleType("browser_harness")
    mod.admin = admin
    mod.helpers = helpers
    with patch.dict("sys.modules", {"browser_harness": mod}):
        result = server._browser_harness()

    assert result == (helpers, admin)
    assert server._bound_cdp == "http://127.0.0.1:9222"


# --- browser_stop / shutdown teardown ---


def test_browser_stop_calls_stop_session_for_agentcore() -> None:
    fake_admin = MagicMock()
    fake_playwright_helpers = MagicMock()
    server._bh = (MagicMock(), fake_admin)
    server._bound_cdp = "agentcore"

    with patch("browser_mcp.playwright_helpers", fake_playwright_helpers):
        server.browser_stop()

    fake_admin.stop_session.assert_called_once()
    fake_playwright_helpers.disconnect.assert_called_once()
    assert server._bh is None
    assert server._bound_cdp is None


def test_browser_stop_does_not_call_stop_session_for_non_agentcore() -> None:
    fake_admin = MagicMock()
    server._bh = (MagicMock(), fake_admin)
    server._bound_cdp = "http://127.0.0.1:9222"

    with patch("browser_harness.admin.restart_daemon") as mock_restart:
        server.browser_stop()

    fake_admin.stop_session.assert_not_called()
    mock_restart.assert_called_once()


def test_browser_stop_stops_leftover_daemon_when_never_bound_here() -> None:
    """Fresh process, _bh never set: browser_stop must still best-effort stop

    browser-harness's daemon, since an external/leftover daemon (e.g. a
    still-billing Browser Use Cloud session from a prior process) may be
    alive -- matching _browser_harness()'s own "Fresh process" comment.
    """
    assert server._bh is None
    with patch("browser_harness.admin.restart_daemon") as mock_restart:
        result = server.browser_stop()

    mock_restart.assert_called_once()
    assert '"ok": true' in result.lower()


def test_browser_stop_returns_error_payload_on_failure() -> None:
    """A failing teardown surfaces {"ok": false, ...}, not a raw traceback --
    matching the convention every other browser_* tool follows on failure."""
    fake_admin = MagicMock()
    fake_admin.stop_session.side_effect = RuntimeError("boom")
    server._bh = (MagicMock(), fake_admin)
    server._bound_cdp = "agentcore"

    with patch("browser_mcp.playwright_helpers", MagicMock()):
        result = server.browser_stop()

    assert '"ok": false' in result.lower()
    assert "boom" in result
    assert server._bound_cdp is None


def test_shutdown_stops_agentcore_session() -> None:
    fake_admin = MagicMock()
    fake_playwright_helpers = MagicMock()
    server._bh = (MagicMock(), fake_admin)
    server._bound_cdp = "agentcore"

    with patch("browser_mcp.playwright_helpers", fake_playwright_helpers):
        server._stop_daemon_for_shutdown()

    fake_admin.stop_session.assert_called_once()
    fake_playwright_helpers.disconnect.assert_called_once()
    assert server._bh is None
    assert server._bound_cdp is None


def test_shutdown_swallows_agentcore_stop_errors() -> None:
    fake_admin = MagicMock()
    fake_admin.stop_session.side_effect = RuntimeError("boom")
    server._bh = (MagicMock(), fake_admin)
    server._bound_cdp = "agentcore"

    server._stop_daemon_for_shutdown()  # must not raise

    assert server._bh is None
    assert server._bound_cdp is None


def test_shutdown_stops_leftover_daemon_when_never_bound_here() -> None:
    """Same "fresh process, external daemon may be alive" safety net as

    browser_stop applies to the atexit/SIGTERM hook too."""
    assert server._bh is None
    with patch("browser_harness.admin.restart_daemon") as mock_restart:
        server._stop_daemon_for_shutdown()
    mock_restart.assert_called_once()


# --- AgentCore reconnect-on-stale-session hook ---


def test_reconnect_agentcore_stops_and_restarts_session() -> None:
    fake_admin = MagicMock()
    fake_admin.ensure_session.return_value = ("wss://fresh/ws", {"Authorization": "sig2"})
    server._agentcore_admin = fake_admin

    result = server._reconnect_agentcore()

    fake_admin.stop_session.assert_called_once()
    fake_admin.ensure_session.assert_called_once()
    assert result == ("wss://fresh/ws", {"Authorization": "sig2"})


def test_agentcore_browser_harness_registers_reconnect_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_BACKEND", "agentcore")
    fake_admin = MagicMock()
    fake_admin.ensure_session.return_value = ("wss://example/ws", {"Authorization": "sig"})
    fake_playwright_helpers = MagicMock()

    with (
        patch("browser_mcp.agentcore.AgentCoreAdmin", return_value=fake_admin),
        patch("browser_mcp.playwright_helpers", fake_playwright_helpers),
    ):
        server._browser_harness()

    fake_playwright_helpers.set_reconnect_hook.assert_called_once_with(server._reconnect_agentcore)
