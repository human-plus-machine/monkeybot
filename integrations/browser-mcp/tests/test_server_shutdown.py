"""Shutdown-hook tests for browser_mcp.server.

Regression coverage for H1: a crashed turn, abandoned conversation, or
container SIGTERM must stop the browser-harness daemon (and its remote
Browser Use Cloud session) instead of leaving it orphaned and billing.
"""

from __future__ import annotations

import signal
from unittest.mock import MagicMock, patch

import pytest
from browser_mcp import server, backend


@pytest.fixture(autouse=True)
def _reset_bh_state():
    """Isolate ``backend._bh`` module state across tests."""
    original = backend._bh
    original_bound = backend._bound_cdp
    backend._bh = None
    backend._bound_cdp = None
    yield
    backend._bh = original
    backend._bound_cdp = original_bound


def test_stop_daemon_for_shutdown_still_stops_daemon_when_never_bound_here() -> None:
    """No daemon bound by this process (_bh is None) -- shutdown must still

    best-effort stop browser-harness's daemon, since a fresh process can't
    tell an external/leftover daemon (e.g. a still-billing Browser Use Cloud
    session from a prior process) apart from "nothing to stop" any other way.
    """
    backend._bh = None
    with patch("browser_harness.admin.restart_daemon") as mock_restart:
        server._stop_daemon_for_shutdown()
    mock_restart.assert_called_once()


def test_stop_daemon_for_shutdown_stops_running_daemon() -> None:
    """A started daemon gets ``restart_daemon()`` called and module state cleared."""
    backend._bh = (MagicMock(), MagicMock())
    with patch("browser_harness.admin.restart_daemon") as mock_restart:
        server._stop_daemon_for_shutdown()
    mock_restart.assert_called_once()
    assert backend._bh is None


def test_stop_daemon_for_shutdown_swallows_errors() -> None:
    """A failing restart_daemon() must not raise -- shutdown must always proceed."""
    backend._bh = (MagicMock(), MagicMock())
    with patch("browser_harness.admin.restart_daemon", side_effect=RuntimeError("boom")):
        server._stop_daemon_for_shutdown()  # must not raise
    assert backend._bh is None


def test_stop_daemon_for_shutdown_is_idempotent() -> None:
    """Calling the hook twice (e.g. atexit + explicit signal handler) is safe.

    Each call best-effort re-attempts restart_daemon() (itself idempotent on
    an already-stopped daemon), so it's called twice here, not once -- the
    guarantee is "safe", not "only ever touches the daemon once".
    """
    backend._bh = (MagicMock(), MagicMock())
    with patch("browser_harness.admin.restart_daemon") as mock_restart:
        server._stop_daemon_for_shutdown()
        server._stop_daemon_for_shutdown()
    assert mock_restart.call_count == 2


def test_install_shutdown_handlers_registers_sigterm_and_sigint() -> None:
    """SIGTERM and SIGINT get a handler that tears down the daemon before exiting."""
    with (
        patch("browser_mcp.server.atexit.register") as mock_atexit,
        patch("browser_mcp.server.signal.signal") as mock_signal,
    ):
        server._install_shutdown_handlers()

    mock_atexit.assert_called_once_with(server._stop_daemon_for_shutdown)
    registered_signals = {call.args[0] for call in mock_signal.call_args_list}
    assert registered_signals == {signal.SIGTERM, signal.SIGINT}


def test_signal_handler_stops_daemon_and_exits_with_signal_code() -> None:
    """The installed handler stops the daemon then exits with the conventional 128+signum code."""
    backend._bh = (MagicMock(), MagicMock())
    captured_handler = {}

    def _capture_signal(sig, handler):
        captured_handler[sig] = handler

    with (
        patch("browser_mcp.server.atexit.register"),
        patch("browser_mcp.server.signal.signal", side_effect=_capture_signal),
    ):
        server._install_shutdown_handlers()

    handler = captured_handler[signal.SIGTERM]
    with (
        patch("browser_harness.admin.restart_daemon") as mock_restart,
        patch("browser_mcp.server.sys.exit") as mock_exit,
    ):
        handler(signal.SIGTERM, None)

    mock_restart.assert_called_once()
    mock_exit.assert_called_once_with(128 + signal.SIGTERM)


def test_install_shutdown_handlers_tolerates_non_main_thread() -> None:
    """signal.signal() raising ValueError (non-main thread) must not propagate."""
    with (
        patch("browser_mcp.server.atexit.register"),
        patch("browser_mcp.server.signal.signal", side_effect=ValueError("not main thread")),
    ):
        server._install_shutdown_handlers()  # must not raise
