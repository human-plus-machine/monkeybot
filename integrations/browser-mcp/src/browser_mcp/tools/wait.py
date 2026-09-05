"""Wait-for-selector and network-idle tools."""

from __future__ import annotations

from browser_mcp import actions
from browser_mcp.app import mcp, _public_tool, prepare_handle
from browser_mcp import results


@mcp.tool()
@_public_tool
def browser_wait_for(
    selector: str, visible: bool = False, timeout: float = 10.0, tab: str | None = None
) -> str:
    """Wait until an element matching selector exists (optionally visible)."""
    prep = prepare_handle(tab)
    if prep.error:
        return prep.error
    handle = prep.handle
    return results.json_text(
        actions.do_wait_for(handle, selector, visible=visible, timeout=timeout)
    )

@mcp.tool()
@_public_tool
def browser_wait_idle(
    timeout: float = 10.0, idle_ms: float = 500, tab: str | None = None
) -> str:
    """Wait until network activity is idle, then until the DOM is quiet.

    Network idle is only available on the focused tab. On another tab this falls
    back to a DOM settle and reports idle as null.
    """
    prep = prepare_handle(tab)
    if prep.error:
        return prep.error
    handle = prep.handle
    return results.json_text(actions.do_wait_idle(handle, timeout=timeout, idle_ms=idle_ms))
