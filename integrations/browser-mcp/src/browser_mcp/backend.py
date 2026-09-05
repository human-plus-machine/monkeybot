"""Browser backend binding: in-app CDP, local harness daemon, or AgentCore."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from browser_mcp import agentcore, dom_indexing, in_app_cdp, perf, tabs

logger = logging.getLogger(__name__)

_bh: tuple[Any, Any] | None = None
# CDP endpoint (BU_CDP_URL/WS or in-app file) the current daemon binding was ensured with,
# or the literal "agentcore" when bound to the AgentCore backend.
# Used instead of browser-harness's nonexistent daemon_browser_kind() to decide when to bounce.
_bound_cdp: str | None = None
_agentcore_admin: agentcore.AgentCoreAdmin | None = None


def mark_unbound() -> None:
    global _bound_cdp
    _bound_cdp = None


def teardown_bound_backend() -> None:
    """Tear down whatever backend _bh is currently bound to, if any.

    Clears ``_bh`` before attempting the (possibly failing) teardown call, so a
    raised exception here never leaves stale backend state behind for the next
    ``browser_harness()`` call to mistakenly reuse. Dispatches on ``_bound_cdp``
    rather than introspecting ``_bh``'s admin object, since the non-agentcore
    path always re-imports (and stops) the real ``browser_harness.admin``
    module regardless of what's stored in ``_bh``.
    """
    global _bh
    if _bh is None:
        return
    helpers, admin = _bh
    is_agentcore = _bound_cdp == "agentcore"
    with contextlib.suppress(Exception):
        tabs.registry().detach_all(helpers)
    _bh = None
    dom_indexing.clear_registered_targets()
    if is_agentcore:
        admin.stop_session()
        from browser_mcp import playwright_helpers

        playwright_helpers.disconnect()
    else:
        from browser_harness import admin as bh_admin

        bh_admin.restart_daemon()


def _with_perf_helpers(bh: tuple[Any, Any]) -> tuple[Any, Any]:
    helpers, admin = bh
    return perf.wrap_helpers(helpers), admin


def _reconnect_agentcore() -> tuple[str, dict[str, str]]:
    """Force a fresh AgentCore session (stop + restart) and return new ws creds.

    Registered with playwright_helpers as its reconnect hook: a stale/expired
    AgentCore session (~15-30 min TTL) leaves the old ws connection dead, and
    plain ensure_session() would just re-sign headers for the same (already
    dead) session, so the old session is explicitly stopped first.
    """
    assert _agentcore_admin is not None
    _agentcore_admin.stop_session()
    return _agentcore_admin.ensure_session()


def _agentcore_browser_harness() -> tuple[Any, Any]:
    """Bind _bh to the AgentCore backend (StartBrowserSession + Playwright CDP connect)."""
    global _bh, _bound_cdp, _agentcore_admin
    from browser_mcp import playwright_helpers

    if _agentcore_admin is None:
        _agentcore_admin = agentcore.AgentCoreAdmin(agentcore.resolve_region())

    ws_url, headers = _agentcore_admin.ensure_session()
    playwright_helpers.connect(ws_url, headers)
    playwright_helpers.set_reconnect_hook(_reconnect_agentcore)
    _bh = (playwright_helpers, _agentcore_admin)
    _bound_cdp = "agentcore"
    return _bh


def browser_harness() -> tuple[Any, Any]:
    """Lazy import + daemon bootstrap on first browser tool use.

    When an explicit CDP URL is configured (env or Monkeyapp runtime file) and the
    live daemon was bound to a different endpoint (or none — i.e. local Chrome),
    bounce it so tool calls drive the in-app panel instead. BROWSER_BACKEND=agentcore
    (with no explicit CDP endpoint) dispatches to AWS Bedrock AgentCore Browser instead.
    """
    global _bh, _bound_cdp
    cdp = in_app_cdp._apply_in_app_cdp_url()

    if agentcore.agentcore_backend_requested():
        if _bh is not None and _bound_cdp == "agentcore":
            return _with_perf_helpers(_bh)
        teardown_bound_backend()
        return _with_perf_helpers(_agentcore_browser_harness())

    if _bh is not None and cdp == _bound_cdp:
        return _with_perf_helpers(_bh)

    if _bound_cdp == "agentcore":
        teardown_bound_backend()

    from browser_harness import admin, helpers

    # Fresh process: _bound_cdp is None while an external local-Chrome daemon may
    # still be alive. Restart whenever the desired CDP differs from what we last
    # ensured (browser-harness 0.1.3 has no daemon_browser_kind() to query).
    if admin.daemon_alive() and _bound_cdp != cdp:
        logger.info(
            "browser-mcp: replacing harness daemon for CDP %s (was %s)",
            in_app_cdp._redact_cdp_token(str(cdp)) if cdp else cdp,
            in_app_cdp._redact_cdp_token(str(_bound_cdp)) if _bound_cdp else _bound_cdp,
        )
        admin.restart_daemon()
        _bh = None
    admin.ensure_daemon()
    _bh = (helpers, admin)
    _bound_cdp = cdp
    return _with_perf_helpers(_bh)


def stop_active_backend_best_effort() -> None:
    """Stop whatever backend may be active, matching the pre-agentcore contract
    that browser_stop / shutdown always best-effort stop the browser-harness
    daemon -- even in a fresh process where ``_bh`` was never bound here, since
    an external/leftover daemon (e.g. a still-billing Browser Use Cloud session
    from a prior process) may still be alive (see ``browser_harness()``'s
    "Fresh process" comment). AgentCore sessions are only ever started by this
    process, so those are only stopped when actually bound.
    """
    global _bh
    from browser_mcp import tab_ops

    if _bound_cdp == "agentcore":
        if _bh is not None:
            helpers, _ = _bh
            with contextlib.suppress(Exception):
                tab_ops._close_agent_opened_tabs(helpers)
        teardown_bound_backend()
        return
    if _bh is not None:
        helpers, _ = _bh
        with contextlib.suppress(Exception):
            tab_ops._close_agent_opened_tabs(helpers)
    _bh = None
    from browser_harness import admin

    admin.restart_daemon()
