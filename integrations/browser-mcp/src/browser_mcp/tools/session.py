"""Login and backend stop."""

from __future__ import annotations

import logging

from browser_mcp import backend, in_app_cdp, login
from browser_mcp.app import mcp, _public_tool
from browser_mcp import results

logger = logging.getLogger(__name__)

@mcp.tool()
@_public_tool
def browser_login(username: str | None = None, expected_origin: str | None = None) -> str:
    """Log in with a saved Spaces password without revealing the credential.

    Call this only when the user asked to sign in. Do not type or read the
    password. Returns {ok, loggedIn, origin} — never the password value.

    This signs in to the tab the user has focused, which is not necessarily the
    tab your other browser_* calls address. Pass expected_origin (e.g.
    "https://example.com") to make the bridge refuse rather than sign in to a
    different site; always check the returned origin before reporting success.
    """
    result = login._sealed_login(username, expected_origin)
    return results.json_text(result)

@mcp.tool()
@_public_tool
def browser_stop() -> str:
    """Stop the active browser backend (cleanup after browsing; important for cloud/AgentCore browsers)."""
    try:
        backend.stop_active_backend_best_effort()
    except Exception as exc:
        backend.mark_unbound()
        # Log the message rather than exc_info: a harness traceback carries the
        # tokenized endpoint, and this log is not necessarily private.
        message = in_app_cdp._redact_cdp_token(str(exc))
        logger.warning("browser_stop: failed to stop browser backend: %s", message)
        return results.json_text({"ok": False, "error": message})
    backend.mark_unbound()
    return results.json_text({"ok": True, "message": "browser backend stopped"})
