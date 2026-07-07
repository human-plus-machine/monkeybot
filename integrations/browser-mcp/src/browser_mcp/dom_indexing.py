"""Text-based DOM indexing (vendored from alibaba/page-agent) for browser-mcp.

Replaces screenshot+coordinate guessing with an indexed, text-only element tree:
CDP-inject a pure DOM-walker script, ask it for a simplified `[index]<tag>text</tag>`
tree, then act on elements by index (click/type/select) instead of pixel coordinates.

Vendored assets (MIT licensed, see NOTICE.md):
- assets/dom_tree.js  - the interactive-element walker, ported from browser-use via
  page-agent's packages/page-controller/src/dom/dom_tree/index.js, unmodified aside
  from converting its ESM `export default` into a plain `window.__bmcpBuildDomTree`
  assignment so it can run as a raw CDP-injected script (no bundler/module loader
  available on the target page).
- assets/pa_driver.js - glue that flattens the tree to text and exposes
  window.__bmcp.{getTree,getRect,getInputInfo,selectOption}, ported from
  page-agent's packages/page-controller/src/dom/index.ts (flatTreeToString,
  getSelectorMap) and actions.ts (selectOptionElement).

Element indices are only valid until the next navigation or DOM mutation big enough
to change the tree; call browser_get_elements again after any action that might
have changed the page. A stale/unknown index raises ElementNotFoundError.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"

_DOM_TREE_JS = (_ASSETS_DIR / "dom_tree.js").read_text(encoding="utf-8")
_DRIVER_JS = (_ASSETS_DIR / "pa_driver.js").read_text(encoding="utf-8")

# Injected once per page/tab; window.__bmcp is the presence marker. Re-injecting is
# harmless (both scripts are idempotent re-assignments) but wasteful over CDP.
_INJECT_GUARD = "if (!window.__bmcp) {" + _DOM_TREE_JS + "\n" + _DRIVER_JS + "\n}"


class ElementNotFoundError(RuntimeError):
    """Raised when an index has no matching element in the page's current selector map."""


def _call(helpers: Any, expression: str) -> Any:
    """Evaluate a window.__bmcp.* expression, assuming it's already injected.

    Only get_elements() injects the ~54KB driver script; every other call here is a
    short follow-up against the page's cached selector map, so we don't want to
    resend the full script on every click/type/select.
    """
    try:
        return helpers.js(expression)
    except RuntimeError as exc:
        if "No element at index" in str(exc) or "window.__bmcp" in str(exc):
            raise ElementNotFoundError(
                "Element index not found (stale or DOM changed). Call browser_get_elements again."
            ) from exc
        raise


def get_elements(helpers: Any, viewport_only: bool) -> dict:
    """Inject the driver (if not already present) and return the indexed
    interactive-element tree as simplified text.

    Caches an index -> element map in the page (window.__bmcpSelectorMap) that
    subsequent click/input/select-by-index calls read from.
    """
    expression = f"{_INJECT_GUARD}\nwindow.__bmcp.getTree({json.dumps(viewport_only)})"
    return helpers.js(expression)


def get_rect(helpers: Any, index: int) -> dict:
    """Bounding-box center for an indexed element (scrolls it into view first)."""
    return _call(helpers, f"window.__bmcp.getRect({json.dumps(index)})")


def get_input_info(helpers: Any, index: int) -> dict:
    """Validate an indexed text input/textarea/contenteditable element and return a
    CSS selector for it (a temporary `data-bmcp-idx` attribute), for use with
    browser-harness's `fill_input`."""
    return _call(helpers, f"window.__bmcp.getInputInfo({json.dumps(index)})")


def select_option(helpers: Any, index: int, option_text: str) -> bool:
    """Set a <select> element's value by visible option text."""
    return _call(
        helpers, f"window.__bmcp.selectOption({json.dumps(index)}, {json.dumps(option_text)})"
    )
