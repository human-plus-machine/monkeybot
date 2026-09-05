"""Site playbook list/read/write/run."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from browser_mcp import actions, playbooks
from browser_mcp.app import mcp, _public_tool, prepare_action
from browser_mcp import results
from browser_mcp.observe import observe_after
from browser_mcp.tools.batch import _act_context

logger = logging.getLogger(__name__)


def _playbook_hints(url: str | None) -> dict[str, Any]:
    key = url or ""
    try:
        return {
            "playbooks": playbooks.list_playbook_names(key) if key else playbooks.list_playbook_names(),
            "flows": playbooks.list_flows(key) if key else playbooks.list_flows(),
        }
    except playbooks.PlaybookError:
        return {"playbooks": [], "flows": []}

def _playbook_timeout_s() -> float:
    raw = (os.environ.get("BROWSER_MCP_PLAYBOOK_TIMEOUT_S") or "").strip()
    if not raw:
        return 120.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 120.0

@mcp.tool()
@_public_tool
def browser_list_playbooks(host: str | None = None) -> str:
    """List playbook markdown filenames and executable flows, optionally filtered by host."""
    try:
        hints = _playbook_hints(host)
    except playbooks.PlaybookError as exc:
        return results.json_text({"ok": False, "error": str(exc)})
    return results.json_text(
        {
            "ok": True,
            "playbooks_dir": str(playbooks.playbooks_dir()),
            **hints,
        }
    )

@mcp.tool()
@_public_tool
def browser_read_playbook(host: str) -> str:
    """Read the playbook markdown for a host or URL."""
    try:
        content = playbooks.read_playbook(host)
    except playbooks.PlaybookError as exc:
        logger.warning("browser_read_playbook failed for host=%r: %s", host, exc)
        return results.json_text({"ok": False, "error": str(exc)})
    return results.json_text({"ok": True, "host": playbooks.host_slug(host), "content": content})

@mcp.tool()
@_public_tool
def browser_write_playbook(host: str, content: str, append: bool = False) -> str:
    """Write or append a site playbook. ```playbook fences are validated before save."""
    try:
        result = playbooks.write_playbook(host, content, append=append)
    except playbooks.PlaybookError as exc:
        logger.warning("browser_write_playbook failed for host=%r: %s", host, exc)
        return results.json_text({"ok": False, "error": str(exc)})
    return results.json_text(result)

@mcp.tool()
@_public_tool
def browser_run_playbook(
    host: str,
    name: str,
    params: dict[str, str] | None = None,
    observe: str | None = None,
    tab: str | None = None,
) -> str:
    """Execute a named ```playbook flow from the host markdown file.

    Substitutes {{param}} into string fields, runs steps via browser_act, then
    checks expect (url_contains, selector, text). On failure returns failed_step
    plus an observation so you can continue by hand. Secrets are never params —
    use {do: login, expected_origin: ...} which maps to browser_login (the
    user-focused tab). Capped by BROWSER_MCP_PLAYBOOK_TIMEOUT_S (default 120).
    """
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return results.json_text({"ok": False, "error": "params must be an object"})
    try:
        flow = playbooks.load_flow(host, name)
        steps = playbooks.substitute_params(flow, params)
    except playbooks.PlaybookError as exc:
        return results.json_text({"ok": False, "error": str(exc), "name": name})
    prep = prepare_action(
        tab, observe=observe, default="diff", focus=True, capture_url=True
    )
    if prep.error:
        return prep.error
    handle = prep.handle
    ctx = _act_context(prep.helpers, handle)
    deadline = time.monotonic() + _playbook_timeout_s()
    executed = actions.execute_steps(ctx, steps, stop_on_error=True, deadline=deadline)
    handle = executed.pop("handle", ctx.handle)
    wrapped = observe_after(
        handle,
        prep.mode,
        {"type": "run_playbook", "name": name, "steps": len(steps)},
        before_url=prep.before_url,
    )
    payload = {k: v for k, v in executed.items() if k != "handle"}
    payload["name"] = name
    if payload.get("ok"):
        payload["completed"] = payload.pop("steps", [])
        expect_error = playbooks.check_expect(handle, flow.expect)
        if expect_error:
            payload["ok"] = False
            payload["error"] = expect_error
    return results.json_text(results.with_observation(payload, wrapped))
