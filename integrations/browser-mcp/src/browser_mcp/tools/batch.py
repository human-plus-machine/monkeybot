"""Multi-step act."""

from __future__ import annotations

from typing import Any

from browser_mcp import actions, login, tab_ops, tabs
from browser_mcp.app import mcp, _public_tool, prepare_action
from browser_mcp import results
from browser_mcp.observe import observe_after


def _act_context(helpers: Any, handle: tabs.TabHandle) -> actions.ActContext:
    def _open(url: str, *, alias: str | None = None, focus: bool = False) -> dict[str, Any]:
        return tab_ops._open_tab(helpers, url, alias=alias, focus=focus)

    return actions.ActContext(
        helpers=helpers,
        handle=handle,
        for_action=lambda name: tab_ops._for_action(helpers, name),
        open_tab=_open,
        login=login._sealed_login,
    )

@mcp.tool()
@_public_tool
def browser_act(
    steps: list[dict[str, Any]],
    observe: str | None = None,
    stop_on_error: bool = True,
    tab: str | None = None,
) -> str:
    """Run up to 25 sequential browser steps in one turn.

    Each step is ``{do, ...}``. Allowed do values: click, input, select, press,
    click_text, wait_for, wait_idle, goto, scroll, settle, tab, open_tab,
    fill_form, login. After each action the page settles; one observation is
    returned at the end for the focused tab. On the first failure
    (stop_on_error=True, default) returns completed steps, failed_step, error,
    and the current observation so you can resume. login maps to browser_login
    (user-focused tab; always pass expected_origin). fill_form resolves fields
    by label (label[for], aria-label, placeholder, name, id, preceding row text).
    """
    validated = actions.validate_steps(steps)
    if isinstance(validated, dict):
        return results.json_text(validated)
    prep = prepare_action(
        tab, observe=observe, default="diff", focus=True, capture_url=True
    )
    if prep.error:
        return prep.error
    ctx = _act_context(prep.helpers, prep.handle)
    executed = actions.execute_steps(ctx, validated, stop_on_error=stop_on_error)
    handle = executed.pop("handle", ctx.handle)
    wrapped = observe_after(
        handle,
        prep.mode,
        {"type": "act", "steps": len(validated)},
        before_url=prep.before_url,
    )
    payload = {k: v for k, v in executed.items() if k != "handle"}
    return results.json_text(results.with_observation(payload, wrapped))
