"""Read-only page tools: tree, text, JS, extract, page_info."""

from __future__ import annotations

from browser_mcp import dom_indexing
from browser_mcp.app import mcp, _public_tool, prepare_handle
from browser_mcp import results
from browser_mcp.observe import _resolve_viewport_only, snapshot_tree


@mcp.tool()
@_public_tool
def browser_get_elements(
    viewport_only: bool | None = None,
    kind: str | None = None,
    contains: str | None = None,
    max_elements: int = 150,
    observe: str = "full",
    tab: str | None = None,
) -> str:
    """Return interactive elements as an indexed text tree — the default way to see the page.

    Prefer this over browser_screenshot for locating things to click/type into. Injects a
    DOM-walker (vendored from alibaba/page-agent, MIT licensed) that scans the live DOM and
    returns lines like ``[12]<input placeholder='Email' />`` / ``[35]<button>Submit</button>``,
    indentation shows parent/child nesting. Use the numeric index with browser_click_by_index /
    browser_input_by_index / browser_select_by_index — no coordinates needed.

    Defaults to the visible viewport (override with viewport_only=false or
    BROWSER_MCP_VIEWPORT_DEFAULT=0). kind is inputs/buttons/links/all; contains is a
    case-insensitive substring on the rendered line (searches the whole page, including
    below the fold); max_elements (default 150) truncates with a footer. Indices remain
    valid until navigation — re-call after navigation, or when the footer says to scroll.
    observe="diff" returns added/removed lines vs the last tree for this tab (falls back
    to a full tree after navigation or with no cache). Pass tab= to read a background tab
    without moving focus.
    """
    kind_norm: str | None = None
    if kind is not None and str(kind).strip():
        kind_norm = str(kind).strip().lower()
        if kind_norm not in results._ELEMENT_KINDS:
            return results.json_text(
                {
                    "ok": False,
                    "error": (
                        f"unknown kind {kind!r}; expected inputs, buttons, links, or all"
                    ),
                }
            )
        if kind_norm == "all":
            kind_norm = None
    observe_norm = (observe or "full").strip().lower()
    if observe_norm not in results._OBSERVE_MODES:
        return results.observe_error(observe, results._OBSERVE_MODES)
    try:
        cap = max(1, int(max_elements))
    except (TypeError, ValueError):
        return results.json_text({"ok": False, "error": "max_elements must be an integer"})
    viewport = _resolve_viewport_only(viewport_only)
    prep = prepare_handle(tab)
    if prep.error:
        return prep.error
    handle = prep.handle
    snap = snapshot_tree(
        handle,
        observe_norm,
        viewport_only=viewport,
        kind=kind_norm,
        contains=contains,
        max_elements=cap,
    )
    if snap.get("error"):
        return results.json_text({"ok": False, "error": snap["error"]})
    payload = {"ok": True, **snap["observation"]}
    if observe_norm != "diff":
        payload.pop("mode", None)
    return results.json_text(payload)

@mcp.tool()
@_public_tool
def browser_get_text(
    max_chars: int = 8000,
    selector: str | None = None,
    tab: str | None = None,
) -> str:
    """Readable page text for reading, separate from the interactive element tree.

    Prefers ``<main>`` / ``<article>`` / ``[role=main]``, else ``body``. Strips
    script, style, nav, footer, and aside. Pass selector to read a specific
    subtree. Use this instead of ``browser_js("document.body.innerText")``.
    """
    try:
        cap = max(1, int(max_chars))
    except (TypeError, ValueError):
        return results.json_text({"ok": False, "error": "max_chars must be an integer"})
    prep = prepare_handle(tab)
    if prep.error:
        return prep.error
    handle = prep.handle
    text = handle.readable_text(selector=selector)
    truncated = len(text) > cap
    if truncated:
        text = text[:cap]
    info = handle.page_info()
    return results.json_text(
        {
            "ok": True,
            "text": text,
            "truncated": truncated,
            "url": info.get("url") or "",
            "title": info.get("title") or "",
        }
    )

@mcp.tool()
@_public_tool
def browser_js(expression: str, tab: str | None = None) -> str:
    """Evaluate JavaScript in the attached tab and return the result (DOM read/extraction)."""
    prep = prepare_handle(tab)
    if prep.error:
        return prep.error
    handle = prep.handle
    result = handle.evaluate(expression)
    return results.json_text({"ok": True, "result": result})

@mcp.tool()
@_public_tool
def browser_extract(
    selector: str,
    fields: dict[str, str],
    limit: int = 50,
    tab: str | None = None,
) -> str:
    """Extract structured rows from elements matching ``selector``.

    Each field is a relative sub-selector (``\"title\": \"h2\"``). Append
    ``@attr`` for an attribute (``\"href\": \"a@href\"``). Missing nodes are
    null. Use this instead of ad-hoc ``browser_js`` scraping.
    """
    if not isinstance(fields, dict) or not fields:
        return results.json_text({"ok": False, "error": "fields must be a non-empty object of name → selector"})
    try:
        cap = max(1, int(limit))
    except (TypeError, ValueError):
        return results.json_text({"ok": False, "error": "limit must be an integer"})
    prep = prepare_handle(tab)
    if prep.error:
        return prep.error
    handle = prep.handle
    result = dom_indexing.extract_rows(handle, selector, fields, limit=cap)
    if result.get("error"):
        return results.json_text({"ok": False, "error": result["error"]})
    return results.json_text(
        {
            "ok": True,
            "rows": result.get("rows") or [],
            "truncated": bool(result.get("truncated")),
        }
    )

@mcp.tool()
@_public_tool
def browser_page_info(tab: str | None = None) -> str:
    """Return current page url, title, viewport size, and scroll position."""
    prep = prepare_handle(tab)
    if prep.error:
        return prep.error
    handle = prep.handle
    return results.json_text(handle.page_info())
