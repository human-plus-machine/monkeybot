"""Stdio MCP server: browser-harness tools + agent-writable site playbooks."""

from __future__ import annotations

import atexit
import contextlib
import logging
import signal
import sys

from browser_mcp.app import mcp
from browser_mcp import backend, in_app_cdp
from browser_mcp.tools import (  # noqa: F401
    browser_act,
    browser_click,
    browser_click_by_index,
    browser_click_text,
    browser_close_tab,
    browser_extract,
    browser_fill,
    browser_fill_form,
    browser_get_elements,
    browser_get_text,
    browser_goto,
    browser_input_by_index,
    browser_js,
    browser_list_playbooks,
    browser_login,
    browser_open_tab,
    browser_page_info,
    browser_press_key,
    browser_read_playbook,
    browser_read_tabs,
    browser_recent_actions,
    browser_run_playbook,
    browser_screenshot,
    browser_scroll,
    browser_select_by_index,
    browser_stop,
    browser_switch_tab,
    browser_tabs,
    browser_upload,
    browser_wait_for,
    browser_wait_idle,
    browser_write_playbook,
)

logger = logging.getLogger(__name__)


def _stop_daemon_for_shutdown() -> None:
    """Best-effort backend stop on process exit (SIGTERM/SIGINT/atexit).

    A crashed turn, abandoned conversation, or container SIGTERM can end this
    stdio process without the model ever calling ``browser_stop``. Closing this
    process's own stdio pipes never reaches a detached browser-harness daemon
    (started via ``start_new_session=True``) or a live AgentCore session, so
    without this hook a remote Browser Use Cloud / AgentCore session -- and its
    billing -- would keep running indefinitely. Always attempts the stop, even
    if this process never itself bound a backend (see
    ``backend.stop_active_backend_best_effort``'s docstring). Idempotent.
    """
    try:
        backend.stop_active_backend_best_effort()
    except Exception as exc:
        logger.warning(
            "browser-mcp shutdown: failed to stop browser backend: %s",
            in_app_cdp._redact_cdp_token(str(exc)),
        )
    backend.mark_unbound()


def _install_shutdown_handlers() -> None:
    """Register atexit + SIGTERM/SIGINT hooks so daemon teardown survives process exit.

    SIGKILL cannot be intercepted by any process (OS-level guarantee) -- this
    only closes the gap for SIGTERM, which orchestrators (Kubernetes, Docker,
    ECS) send first with a grace period before escalating to SIGKILL.
    """
    atexit.register(_stop_daemon_for_shutdown)

    def _handle_signal(signum: int, _frame: object) -> None:
        _stop_daemon_for_shutdown()
        sys.exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        # Not the main thread, or unsupported on this platform -- atexit still
        # covers normal interpreter shutdown in that case.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _handle_signal)


def main() -> None:
    _install_shutdown_handlers()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
