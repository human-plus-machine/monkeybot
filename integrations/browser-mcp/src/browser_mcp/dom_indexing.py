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

Element indices remain valid until navigation. After navigation call
browser_get_elements again. A stale/unknown index raises ElementNotFoundError.

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
import difflib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from browser_mcp.tabs import TabHandle, as_handle, registry, reset_registry

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
_STALE_INDEX_MSG = (
    "Element index not found (stale or navigated). "
    "Call browser_get_elements again after navigation."
)
_FOOTER_BELOW = (
    "… {n} more interactive elements below the viewport "
    "(scroll or pass viewport_only=false)"
)
_FOOTER_TRUNCATED = "… truncated, {k} elements omitted; use contains= or scroll"
# helpers._send uses a 5s socket timeout; each awaited JS wait must stay under it.
_IPC_WAIT_CHUNK_S = 4.0
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


def _helpers_of(target: Any) -> Any:
    return target.helpers if isinstance(target, TabHandle) else target


def _target_key(target: Any) -> str:
    if isinstance(target, TabHandle) and target.state is not None:
        return target.state.target_id
    helpers = _helpers_of(target)
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


def _mark_registered(handle: TabHandle) -> None:
    global _driver_ready
    if handle.state is not None:
        handle.state.driver_registered = True
        return
    key = _target_key(handle) or "_default"
    _registered_targets.add(key)
    _driver_ready = True


def clear_registered_targets() -> None:
    """Drop per-target registration (call on backend teardown)."""
    global _driver_ready
    _registered_targets.clear()
    _driver_ready = False
    reset_registry()


def mark_driver_stale(target: Any | None = None) -> None:
    """Current document may not have the driver (new tab / switch)."""
    global _driver_ready
    if isinstance(target, TabHandle) and target.state is not None:
        target.state.driver_registered = False
        return
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


def _register_via_cdp(handle: TabHandle) -> None:
    for chunk in _b64_chunks(_DRIVER_SOURCE):
        source = _cdp_chunk_script(chunk)
        payload = json.dumps({"source": source})
        if len(payload) >= _CDP_SOURCE_JSON_LIMIT:
            raise RuntimeError("DOM driver registration chunk exceeds IPC JSON limit")
        handle.cdp("Page.addScriptToEvaluateOnNewDocument", source=source)
    handle.cdp("Page.addScriptToEvaluateOnNewDocument", source=_CDP_JOIN_SCRIPT)


def _register_via_init_script(handle: TabHandle) -> None:
    helpers = handle.helpers
    helpers.add_init_script(_wrapped_driver_source())
    registry().init_script_registered = True


def _inject_current(target: Any) -> None:
    """Chunked eval of the driver into the already-loaded document."""
    handle = as_handle(target)
    handle.evaluate("window.__bmcpChunks = []")
    for chunk in _b64_chunks(_DRIVER_SOURCE):
        handle.evaluate(f"window.__bmcpChunks.push({json.dumps(chunk)})")
    handle.evaluate(_INJECT_EVAL_JS)


def _register_driver_for_new_documents(target: Any) -> None:
    """Persist the driver for future documents on this target; inject the current one."""
    global _driver_ready
    handle = as_handle(target)
    if handle.state is not None:
        if handle.state.driver_registered:
            return
    elif _driver_ready:
        return

    if handle.state is None:
        key = _target_key(handle) or "_default"
        if key in _registered_targets:
            _driver_ready = True
            return

    helpers = handle.helpers
    persisted = False
    if _has_helper(helpers, "cdp"):
        _register_via_cdp(handle)
        persisted = True
    elif _has_helper(helpers, "add_init_script"):
        if not registry().init_script_registered:
            _register_via_init_script(handle)
        persisted = True

    if persisted:
        _inject_current(handle)
        _mark_registered(handle)
        return

    if handle.evaluate("!!window.__bmcp") is True:
        _mark_registered(handle)
        return
    _inject_current(handle)
    _mark_registered(handle)


def _ensure_driver(target: Any) -> None:
    """Inject the DOM driver if ``window.__bmcp`` is missing (chunked for IPC limits)."""
    _register_driver_for_new_documents(target)


def _call(target: Any, expression: str) -> Any:
    """Evaluate a window.__bmcp.* expression, assuming it's already injected.

    Only get_elements() injects the driver script; every other call here is a
    short follow-up against the page's cached selector map, so we don't want to
    resend the full script on every click/type/select.
    """
    handle = as_handle(target)
    try:
        return handle.evaluate(expression)
    except RuntimeError as exc:
        if "No element at index" in str(exc) or "window.__bmcp" in str(exc):
            raise ElementNotFoundError(_STALE_INDEX_MSG) from exc
        raise


def _inject_error(target: Any) -> str | None:
    handle = as_handle(target)
    try:
        err = handle.evaluate("window.__bmcpInjectError || null")
    except RuntimeError:
        return None
    return str(err) if err else None


def get_tree_expr(
    viewport_only: bool,
    *,
    kind: str | None = None,
    contains: str | None = None,
    max_elements: int = 150,
) -> str:
    payload = {
        "viewportOnly": bool(viewport_only),
        "kind": kind,
        "contains": contains,
        "maxElements": max(1, int(max_elements)),
    }
    return f"window.__bmcp.getTree({json.dumps(payload)})"


def tree_lines(tree: str) -> list[str]:
    return list((tree or "").splitlines())


def diff_tree_lines(previous: list[str], current: list[str]) -> dict[str, Any]:
    """Line diff of two rendered trees. Unchanged lines compare equal (stable indices)."""
    matcher = difflib.SequenceMatcher(a=previous, b=current, autojunk=False)
    added: list[str] = []
    removed: list[str] = []
    unchanged = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            unchanged += i2 - i1
        elif tag == "insert":
            added.extend(current[j1:j2])
        elif tag == "delete":
            removed.extend(previous[i1:i2])
        elif tag == "replace":
            removed.extend(previous[i1:i2])
            added.extend(current[j1:j2])
    return {"added": added, "removed": removed, "unchanged": unchanged}


def attach_tree_footers(
    tree: str,
    *,
    viewport_only: bool,
    below_viewport: int,
    truncated: bool,
    omitted: int,
) -> str:
    parts: list[str] = []
    if tree:
        parts.append(tree)
    if viewport_only and below_viewport > 0:
        parts.append(_FOOTER_BELOW.format(n=below_viewport))
    if truncated:
        parts.append(_FOOTER_TRUNCATED.format(k=omitted))
    return "\n".join(parts)


def get_elements(
    target: Any,
    viewport_only: bool = True,
    *,
    kind: str | None = None,
    contains: str | None = None,
    max_elements: int = 150,
) -> dict:
    """Inject the driver (if not already present) and return the indexed
    interactive-element tree as simplified text.

    Walks the full page so indices stay valid for off-screen elements; the
    returned ``tree`` is filtered (viewport / kind / contains / max_elements).
    Caches an index -> element map in the page (window.__bmcpSelectorMap) that
    subsequent click/input/select-by-index calls read from.
    """
    handle = as_handle(target)
    _ensure_driver(handle)
    expr = get_tree_expr(
        viewport_only, kind=kind, contains=contains, max_elements=max_elements
    )
    try:
        result = handle.evaluate(expr)
    except RuntimeError:
        err = _inject_error(handle)
        if err:
            return {"error": err, "tree": "", "elementCount": 0}
        _inject_current(handle)
        try:
            result = handle.evaluate(expr)
        except RuntimeError:
            err = _inject_error(handle)
            if err:
                return {"error": err, "tree": "", "elementCount": 0}
            raise
    if isinstance(result, dict):
        result.setdefault("truncated", False)
        result.setdefault("below_viewport", 0)
        result.setdefault("omitted", 0)
        return result
    return {"tree": "", "elementCount": 0, "truncated": False, "below_viewport": 0, "omitted": 0}


def get_rect(target: Any, index: int) -> dict:
    """Bounding-box center for an indexed element (scrolls it into view first)."""
    return _call(target, f"window.__bmcp.getRect({json.dumps(index)})")


def get_rects(
    target: Any,
    indices: list[int] | None = None,
    *,
    scroll: bool = False,
    full: bool = False,
) -> dict:
    """Top-left boxes for indexed elements. Does not scroll unless ``scroll=True``.

    Returns ``{rects, cssWidth, cssHeight, dpr}``. ``rects`` keys may be strings
    (JSON object keys). Empty / missing maps yield an empty ``rects`` dict.
    """
    expr = (
        f"window.__bmcp.getRects({json.dumps(indices)}, "
        f"{json.dumps({'scroll': scroll, 'full': full})})"
    )
    result = _call(target, expr)
    if isinstance(result, dict) and isinstance(result.get("rects"), dict):
        return result
    return {"rects": {}, "cssWidth": 0, "cssHeight": 0, "dpr": 1}


def get_input_info(target: Any, index: int) -> dict:
    """Validate an indexed text input/textarea/contenteditable element and return a
    CSS selector for it (a temporary `data-bmcp-idx` attribute), for use with
    browser-harness's `fill_input`."""
    return _call(target, f"window.__bmcp.getInputInfo({json.dumps(index)})")


def select_option(target: Any, index: int, option_text: str) -> bool:
    """Set a <select> element's value by visible option text."""
    return _call(
        target, f"window.__bmcp.selectOption({json.dumps(index)}, {json.dumps(option_text)})"
    )


def fill(
    target: Any,
    index: int,
    text: str,
    clear_first: bool = True,
    mode: str = "auto",
) -> dict:
    """Fill an indexed input. ``auto`` tries in-page fill then key-event fallback."""
    handle = as_handle(target)
    if mode not in _FILL_MODES:
        mode = "auto"
    _ensure_driver(handle)
    if mode != "keys":
        result = _try_fast_fill(handle, index, text, clear_first)
        if result is not None:
            if mode == "fast" or (not result.get("needsKeys") and result.get("value") == text):
                result["mode_used"] = "fast"
                return result
    info = get_input_info(handle, index)
    handle.helpers.fill_input(info["selector"], text, clear_first=clear_first)
    return {
        "ok": True,
        "value": text,
        "tagName": info.get("tagName"),
        "mode_used": "keys",
    }


def _try_fast_fill(target: Any, index: int, text: str, clear_first: bool) -> dict | None:
    handle = as_handle(target)
    opts = json.dumps({"clear": clear_first})
    expr = f"window.__bmcp.fill({json.dumps(index)}, {json.dumps(text)}, {opts})"
    try:
        result = handle.evaluate(expr)
    except RuntimeError as exc:
        if "No element at index" in str(exc) or "window.__bmcp" in str(exc):
            raise ElementNotFoundError(_STALE_INDEX_MSG) from exc
        return None
    return result if isinstance(result, dict) else None


def settle(target: Any, quiet_ms: int = 150, max_ms: int = 1500) -> dict:
    """Wait until the DOM is quiet or ``max_ms`` elapses. One awaited JS call."""
    handle = as_handle(target)
    _ensure_driver(handle)
    expr = f"window.__bmcp.settle({json.dumps(quiet_ms)}, {json.dumps(max_ms)})"
    try:
        result = handle.evaluate(expr)
    except RuntimeError as exc:
        msg = str(exc).lower()
        if any(marker in msg for marker in _NAVIGATED_MARKERS):
            return {"quiet": True, "navigated": True}
        if "window.__bmcp" in str(exc):
            _inject_current(handle)
            result = handle.evaluate(expr)
        else:
            raise
    if isinstance(result, dict):
        result.setdefault("navigated", False)
        return result
    return {"quiet": True, "navigated": False}


def _wait_for_expr(selector: str, visible: bool, timeout_s: float) -> str:
    sel = json.dumps(selector)
    vis = "true" if visible else "false"
    ms = int(max(timeout_s, 0.0) * 1000)
    return (
        "(function(){"
        f"const sel={sel};const wantVisible={vis};const timeoutMs={ms};"
        "const check=()=>{"
        "try{"
        "const e=document.querySelector(sel);if(!e)return {found:false};"
        "if(!wantVisible)return {found:true};"
        "if(typeof e.checkVisibility==='function')"
        "return {found:e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})};"
        "const s=getComputedStyle(e);"
        "return {found:s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'};"
        "}catch(err){return {found:false,error:'invalid selector'};}"
        "};"
        "const first=check();"
        "if(first.error||first.found)return first;"
        "if(timeoutMs<=0)return {found:false};"
        "return new Promise((resolve)=>{"
        "let done=false;let observer;"
        "const finish=(value)=>{if(done)return;done=true;"
        "if(observer)observer.disconnect();clearTimeout(timer);resolve(value);};"
        "observer=new MutationObserver(()=>{"
        "const next=check();if(next.error||next.found)finish(next);"
        "});"
        "const timer=setTimeout(()=>finish({found:false}),timeoutMs);"
        "observer.observe(document,{subtree:true,childList:true,attributes:true,characterData:true});"
        "});"
        "})()"
    )


def _as_wait_result(result: Any) -> dict:
    if isinstance(result, dict):
        out: dict[str, Any] = {"found": bool(result.get("found"))}
        if result.get("error"):
            out["error"] = str(result["error"])
        if result.get("navigated"):
            out["navigated"] = True
        return out
    return {"found": bool(result)}


def wait_for_selector(
    target: Any,
    selector: str,
    visible: bool = False,
    timeout: float = 10.0,
) -> dict:
    """Wait for ``selector`` with one awaited MutationObserver per IPC chunk.

    Chunks are capped at 4s so we stay under browser-harness's 5s socket
    read timeout. Does not inject the DOM driver.
    """
    handle = as_handle(target)
    remaining = max(float(timeout), 0.0)
    last: dict[str, Any] = {"found": False}
    while True:
        chunk_s = min(remaining, _IPC_WAIT_CHUNK_S) if remaining > 0 else 0.0
        try:
            result = handle.evaluate(_wait_for_expr(selector, visible, chunk_s))
        except RuntimeError as exc:
            msg = str(exc).lower()
            if any(marker in msg for marker in _NAVIGATED_MARKERS):
                return {"found": False, "navigated": True}
            raise
        last = _as_wait_result(result)
        if last.get("found") or last.get("error") or remaining <= 0:
            return last
        remaining -= chunk_s
        if remaining <= 0:
            return last

