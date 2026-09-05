"""Multi-step act and fill_form."""

from __future__ import annotations

from typing import Any

from browser_mcp import actions, backend, login, tab_ops, tabs
from browser_mcp.app import mcp, _public_tool
from browser_mcp import results
from browser_mcp.observe import observe_after, resolve_action_observe

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
    (user-focused tab; always pass expected_origin).
    """
    mode = resolve_action_observe(observe)
    if mode not in results._ACTION_OBSERVE_MODES:
        return results.observe_error(observe if observe is not None else mode, results._ACTION_OBSERVE_MODES)
    validated = actions.validate_steps(steps)
    if isinstance(validated, dict):
        return results.json_text(validated)
    helpers, _ = backend.browser_harness()
    try:
        handle = tab_ops._for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return results.unknown_tab_result(exc)

    ctx = _act_context(helpers, handle)
    before_url = str(handle.page_info().get("url") or "")
    executed = actions.execute_steps(ctx, validated, stop_on_error=stop_on_error)
    handle = executed.pop("handle", ctx.handle)
    wrapped = observe_after(
        handle,
        mode,
        {"type": "act", "steps": len(validated)},
        before_url=before_url,
    )
    payload = {k: v for k, v in executed.items() if k != "handle"}
    return results.json_text(results.with_observation(payload, wrapped))

@mcp.tool()
@_public_tool
def browser_fill_form(
    fields: dict[str, str],
    submit: bool = False,
    mode: str = "auto",
    observe: str | None = None,
    tab: str | None = None,
) -> str:
    """Fill a form by field label in one call.

    Resolves each key with label[for], aria-label, aria-labelledby, placeholder,
    name, id, then nearest preceding row text (case-insensitive, unique
    substring). Selects for <select>; checks/unchecks checkboxes when the value
    is \"true\"/\"false\". Unresolved labels are listed and are not an error
    unless every field failed. submit=True clicks the form's submit button if
    one exists and is enabled, otherwise presses Enter in the last field.
    The ``how`` field on each filled entry says which strategy matched.
    """
    observe_mode = resolve_action_observe(observe)
    if observe_mode not in results._ACTION_OBSERVE_MODES:
        return results.observe_error(
            observe if observe is not None else observe_mode, results._ACTION_OBSERVE_MODES
        )
    if not isinstance(fields, dict):
        return results.json_text({"ok": False, "error": "fields must be an object of label → value"})
    helpers, _ = backend.browser_harness()
    try:
        handle = tab_ops._for_action(helpers, tab)
    except tabs.UnknownTabError as exc:
        return results.unknown_tab_result(exc)
    before_url = str(handle.page_info().get("url") or "")
    payload = actions.do_fill_form(handle, fields, submit=submit, mode=mode)
    wrapped = observe_after(
        handle,
        observe_mode,
        {"type": "fill_form", "filled": len(payload.get("filled") or [])},
        before_url=before_url,
    )
    return results.json_text(results.with_observation(payload, wrapped))
