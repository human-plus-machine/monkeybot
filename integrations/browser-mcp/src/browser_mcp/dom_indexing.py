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
  window.__bmcp.{getTree,getRect,getRects,getInputInfo,selectOption,fill,settle},
  ported from page-agent's packages/page-controller/src/dom/index.ts
  (flatTreeToString, getSelectorMap) and actions.ts (selectOptionElement).

Element indices are only valid until the next navigation or DOM mutation big enough
to change the tree; call browser_get_elements again after any action that might
have changed the page. A stale/unknown index raises ElementNotFoundError.

Injection note: browser-harness relays CDP over a newline-delimited Unix/TCP socket
whose asyncio StreamReader defaults to a 64 KiB line limit. The combined driver is
~60 KiB, so a single ``helpers.js(full_script)`` call fails with
``Separator is found, but chunk is longer than limit``. We therefore assemble the
script in-page from base64 chunks, each well under that limit. When ``helpers.cdp``
or ``helpers.add_init_script`` is available, those chunks are also registered with
``Page.addScriptToEvaluateOnNewDocument`` so later navigations skip re-injection.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"

_DOM_TREE_JS = (_ASSETS_DIR / "dom_tree.js").read_text(encoding="utf-8")
_DRIVER_JS = (_ASSETS_DIR / "pa_driver.js").read_text(encoding="utf-8")

# Full driver body (no presence guard). Injected via chunked base64 assembly.
_DRIVER_SOURCE = _DOM_TREE_JS + "\n" + _DRIVER_JS

# Keep each IPC JSON line comfortably under asyncio's default 64 KiB StreamReader
# limit (request framing + JSON escaping inflate the on-wire size).
_INJECT_CHUNK_CHARS = 24_000
_CDP_SOURCE_JSON_LIMIT = 60_000
_FILL_MODES = frozenset({"auto", "keys", "fast"})
_NAVIGATED_MARKERS = (
    "context destroyed",
    "inspected target navigated",
    "execution context was destroyed",
)

_INJECT_EVAL_JS = (
    "(() => {"
    "if (window.__bmcp) return;"
    "try {"
    "const src = atob(window.__bmcpChunks.join(''));"
    "delete window.__bmcpChunks;"
    "(0, eval)(src);"
    "} catch (e) {"
    "window.__bmcpInjectError = String((e && e.message) || e);"
    "}"
    "})()"
)

_CDP_JOIN_SCRIPT = (
    "(function(){"
    "if (window.__bmcp) return;"
    "try {"
    "const src = atob((window.__bmcpChunks || []).join(''));"
    "delete window.__bmcpChunks;"
    "(0, eval)(src);"
    "} catch (e) {"
    "window.__bmcpInjectError = String((e && e.message) || e);"
    "}"
    "})()"
)

_registered_targets: set[str] = set()
_driver_ready: bool = False


class ElementNotFoundError(RuntimeError):
    """Raised when an index has no matching element in the page's current selector map."""


def _b64_chunks(source: str, size: int = _INJECT_CHUNK_CHARS) -> list[str]:
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    return [encoded[i : i + size] for i in range(0, len(encoded), size)]


def _has_helper(helpers: Any, name: str) -> bool:
    return callable(getattr(helpers, name, None))


def _target_key(helpers: Any) -> str:
    if _has_helper(helpers, "current_tab"):
        try:
            tab = helpers.current_tab()
        except Exception:
            tab = None
        if isinstance(tab, dict):
            tid = tab.get("targetId") or tab.get("target_id")
            if tid:
                return str(tid)
    if _has_helper(helpers, "page_info"):
        try:
            info = helpers.page_info() or {}
        except Exception:
            info = {}
        url = str(info.get("url") or "")
        return urlparse(url).netloc or url
    return ""


def clear_registered_targets() -> None:
    """Drop per-target registration (call on backend teardown)."""
    global _driver_ready
    _registered_targets.clear()
    _driver_ready = False


def mark_driver_stale() -> None:
    """Current document may not have the driver (new tab / switch)."""
    global _driver_ready
    _driver_ready = False


def _cdp_chunk_script(chunk: str) -> str:
    return (
        "(function(){"
        "if (window.__bmcp) return;"
        "window.__bmcpChunks = window.__bmcpChunks || [];"
        f"window.__bmcpChunks.push({json.dumps(chunk)});"
        "})()"
    )


def _wrapped_driver_source() -> str:
    return (
        "(function(){\n"
        "if (window.__bmcp) return;\n"
        "try {\n"
        f"{_DRIVER_SOURCE}\n"
        "} catch (e) { window.__bmcpInjectError = String((e && e.message) || e); }\n"
        "})();"
    )


def _register_via_cdp(helpers: Any) -> None:
    for chunk in _b64_chunks(_DRIVER_SOURCE):
        source = _cdp_chunk_script(chunk)
        payload = json.dumps({"source": source})
        if len(payload) >= _CDP_SOURCE_JSON_LIMIT:
            raise RuntimeError("DOM driver registration chunk exceeds IPC JSON limit")
        helpers.cdp("Page.addScriptToEvaluateOnNewDocument", source=source)
    helpers.cdp("Page.addScriptToEvaluateOnNewDocument", source=_CDP_JOIN_SCRIPT)


def _register_via_init_script(helpers: Any) -> None:
    helpers.add_init_script(_wrapped_driver_source())


def _inject_current(helpers: Any) -> None:
    """Chunked eval of the driver into the already-loaded document."""
    helpers.js("window.__bmcpChunks = []")
    for chunk in _b64_chunks(_DRIVER_SOURCE):
        helpers.js(f"window.__bmcpChunks.push({json.dumps(chunk)})")
    helpers.js(_INJECT_EVAL_JS)


def _register_driver_for_new_documents(helpers: Any) -> None:
    """Persist the driver for future documents on this target; inject the current one."""
    global _driver_ready
    if _driver_ready:
        return

    key = _target_key(helpers) or "_default"
    if key in _registered_targets:
        _driver_ready = True
        return

    persisted = False
    if _has_helper(helpers, "cdp"):
        _register_via_cdp(helpers)
        persisted = True
    elif _has_helper(helpers, "add_init_script"):
        _register_via_init_script(helpers)
        persisted = True

    if persisted:
        _registered_targets.add(key)
        _inject_current(helpers)
        _driver_ready = True
        return

    if helpers.js("!!window.__bmcp") is True:
        _driver_ready = True
        return
    _inject_current(helpers)
    _driver_ready = True


def _ensure_driver(helpers: Any) -> None:
    """Inject the DOM driver if ``window.__bmcp`` is missing (chunked for IPC limits)."""
    _register_driver_for_new_documents(helpers)


def _call(helpers: Any, expression: str) -> Any:
    """Evaluate a window.__bmcp.* expression, assuming it's already injected.

    Only get_elements() injects the driver script; every other call here is a
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


def _inject_error(helpers: Any) -> str | None:
    try:
        err = helpers.js("window.__bmcpInjectError || null")
    except RuntimeError:
        return None
    return str(err) if err else None


def get_elements(helpers: Any, viewport_only: bool) -> dict:
    """Inject the driver (if not already present) and return the indexed
    interactive-element tree as simplified text.

    Caches an index -> element map in the page (window.__bmcpSelectorMap) that
    subsequent click/input/select-by-index calls read from.
    """
    _ensure_driver(helpers)
    expr = f"window.__bmcp.getTree({json.dumps(viewport_only)})"
    try:
        result = helpers.js(expr)
    except RuntimeError:
        err = _inject_error(helpers)
        if err:
            return {"error": err, "tree": "", "elementCount": 0}
        _inject_current(helpers)
        try:
            result = helpers.js(expr)
        except RuntimeError:
            err = _inject_error(helpers)
            if err:
                return {"error": err, "tree": "", "elementCount": 0}
            raise
    if isinstance(result, dict):
        return result
    return {"tree": "", "elementCount": 0}


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


def fill(
    helpers: Any,
    index: int,
    text: str,
    clear_first: bool = True,
    mode: str = "auto",
) -> dict:
    """Fill an indexed input. ``auto`` tries in-page fill then key-event fallback."""
    if mode not in _FILL_MODES:
        mode = "auto"
    _ensure_driver(helpers)
    if mode != "keys":
        result = _try_fast_fill(helpers, index, text, clear_first)
        if result is not None:
            if mode == "fast" or (not result.get("needsKeys") and result.get("value") == text):
                result["mode_used"] = "fast"
                return result
    info = get_input_info(helpers, index)
    helpers.fill_input(info["selector"], text, clear_first=clear_first)
    return {
        "ok": True,
        "value": text,
        "tagName": info.get("tagName"),
        "mode_used": "keys",
    }


def _try_fast_fill(helpers: Any, index: int, text: str, clear_first: bool) -> dict | None:
    opts = json.dumps({"clear": clear_first})
    expr = f"window.__bmcp.fill({json.dumps(index)}, {json.dumps(text)}, {opts})"
    try:
        result = helpers.js(expr)
    except RuntimeError as exc:
        if "No element at index" in str(exc) or "window.__bmcp" in str(exc):
            raise ElementNotFoundError(
                "Element index not found (stale or DOM changed). Call browser_get_elements again."
            ) from exc
        return None
    return result if isinstance(result, dict) else None


def settle(helpers: Any, quiet_ms: int = 150, max_ms: int = 1500) -> dict:
    """Wait until the DOM is quiet or ``max_ms`` elapses. One awaited JS call."""
    _ensure_driver(helpers)
    expr = f"window.__bmcp.settle({json.dumps(quiet_ms)}, {json.dumps(max_ms)})"
    try:
        result = helpers.js(expr)
    except RuntimeError as exc:
        msg = str(exc).lower()
        if any(marker in msg for marker in _NAVIGATED_MARKERS):
            return {"quiet": True, "navigated": True}
        if "window.__bmcp" in str(exc):
            _inject_current(helpers)
            result = helpers.js(expr)
        else:
            raise
    if isinstance(result, dict):
        result.setdefault("navigated", False)
        return result
    return {"quiet": True, "navigated": False}
