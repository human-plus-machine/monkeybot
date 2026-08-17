"""browser_harness.helpers-compatible driver backed by Playwright, for AgentCore.

Exposes the same free-function surface browser_harness.helpers does (new_tab,
wait_for_load, page_info, click_at_xy, fill_input, press_key, scroll, js,
wait_for_element, wait_for_network_idle, list_tabs, switch_tab, upload_file,
capture_screenshot) so ``_bh = (playwright_helpers, admin)`` slots into every
existing ``helpers, _ = _browser_harness()`` call in server.py / dom_indexing.py
unchanged.

Playwright's sync API is thread-affine: every object it creates must be used
from the thread that started ``sync_playwright()``. FastMCP dispatches sync
``@mcp.tool()`` calls on a thread pool (not necessarily the same OS thread
twice), so all Playwright work is marshalled onto one dedicated background
thread via a single-worker executor -- state (playwright/browser/context/page)
lives only inside that thread's closures, never touched from a caller thread.
"""

from __future__ import annotations

import contextlib
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

_executor: ThreadPoolExecutor | None = None

# Mutated only from inside the dedicated executor thread.
_state: dict[str, Any] = {"playwright": None, "browser": None, "context": None, "page": None}
_tab_ids: dict[int, Any] = {}  # synthesized target_id (id(page)) -> Page


def _ensure_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agentcore-playwright")
    return _executor


def _run(fn: Any, *args: Any, **kwargs: Any) -> Any:
    return _ensure_executor().submit(fn, *args, **kwargs).result()


def _connect_impl(ws_url: str, headers: dict[str, str]) -> None:
    from playwright.sync_api import sync_playwright

    if _state["browser"] is not None:
        _disconnect_impl()

    pw = sync_playwright().start()
    # Playwright's driver rejects an empty headers dict ("expected array, got
    # object") -- only pass it through when non-empty. generate_ws_headers()
    # always returns a populated dict in practice (Host/X-Amz-Date/Authorization/...).
    browser = pw.chromium.connect_over_cdp(ws_url, headers=headers or None)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.pages[0] if context.pages else context.new_page()

    _state["playwright"] = pw
    _state["browser"] = browser
    _state["context"] = context
    _state["page"] = page
    _tab_ids.clear()
    _tab_ids[id(page)] = page


def _disconnect_impl() -> None:
    browser = _state["browser"]
    pw = _state["playwright"]
    _state["playwright"] = None
    _state["browser"] = None
    _state["context"] = None
    _state["page"] = None
    _tab_ids.clear()
    if browser is not None:
        with contextlib.suppress(Exception):
            browser.close()
    if pw is not None:
        with contextlib.suppress(Exception):
            pw.stop()


def connect(ws_url: str, headers: dict[str, str]) -> None:
    """Connect (or reconnect) Playwright to the AgentCore CDP endpoint."""
    _run(_connect_impl, ws_url, headers)


def disconnect() -> None:
    """Close the Playwright browser/connection and reset state. Idempotent."""
    _run(_disconnect_impl)


def _page() -> Any:
    page = _state["page"]
    if page is None:
        raise RuntimeError("playwright_helpers: not connected -- call connect() first")
    return page


# --- navigation / page ---


def _new_tab_impl(url: str) -> str:
    page = _page()
    if url and url != "about:blank":
        page.goto(url)
    return page.url


def new_tab(url: str = "about:blank") -> str:
    return _run(_new_tab_impl, url)


def _wait_for_load_impl() -> bool:
    try:
        _page().wait_for_load_state("load")
        return True
    except Exception:
        return False


def wait_for_load() -> bool:
    return _run(_wait_for_load_impl)


def _page_info_impl() -> dict:
    page = _page()
    expression = (
        "JSON.stringify({url:location.href,title:document.title,w:innerWidth,"
        "h:innerHeight,sx:scrollX,sy:scrollY,"
        "pw:document.documentElement.scrollWidth,ph:document.documentElement.scrollHeight})"
    )
    return json.loads(page.evaluate(expression))


def page_info() -> dict:
    return _run(_page_info_impl)


# --- input ---


def _click_at_xy_impl(x: float, y: float, button: str, clicks: int) -> None:
    _page().mouse.click(x, y, button=button, click_count=clicks)


def click_at_xy(x: float, y: float, button: str = "left", clicks: int = 1) -> None:
    _run(_click_at_xy_impl, x, y, button, clicks)


def _fill_input_impl(selector: str, text: str, clear_first: bool, timeout: float) -> None:
    page = _page()
    if timeout > 0 and not _wait_for_element_impl(selector, timeout, False):
        raise RuntimeError(f"fill_input: element not found: {selector!r}")
    locator = page.locator(selector)
    try:
        locator.wait_for(state="attached", timeout=1000)
    except Exception as exc:
        raise RuntimeError(f"fill_input: element not found: {selector!r}") from exc
    locator.click()
    if clear_first:
        locator.press("Control+A")
        locator.press("Backspace")
    locator.press_sequentially(text)
    page.evaluate(
        "(()=>{const e=document.activeElement;if(!e)return;"
        "e.dispatchEvent(new Event('input',{bubbles:true}));"
        "e.dispatchEvent(new Event('change',{bubbles:true}));})();"
    )


def fill_input(selector: str, text: str, clear_first: bool = True, timeout: float = 0.0) -> None:
    _run(_fill_input_impl, selector, text, clear_first, timeout)


_MODIFIER_NAMES = {1: "Alt", 2: "Control", 4: "Meta", 8: "Shift"}


def _key_combo(key: str, modifiers: int) -> str:
    parts = [name for bit, name in _MODIFIER_NAMES.items() if modifiers & bit]
    parts.append(key)
    return "+".join(parts)


def _press_key_impl(key: str, modifiers: int) -> None:
    _page().keyboard.press(_key_combo(key, modifiers))


def press_key(key: str, modifiers: int = 0) -> None:
    _run(_press_key_impl, key, modifiers)


def _scroll_impl(x: float, y: float, dy: float, dx: float) -> None:
    page = _page()
    page.mouse.move(x, y)
    page.mouse.wheel(dx, dy)


def scroll(x: float, y: float, dy: float = -300, dx: float = 0) -> None:
    _run(_scroll_impl, x, y, dy, dx)


def _js_impl(expression: str) -> Any:
    from playwright.sync_api import Error as PlaywrightError

    try:
        return _page().evaluate(expression)
    except PlaywrightError as exc:
        # Re-raise as RuntimeError so dom_indexing.py's existing
        # `except RuntimeError` message-sniffing keeps working unchanged.
        raise RuntimeError(str(exc)) from exc


def js(expression: str) -> Any:
    return _run(_js_impl, expression)


def _wait_for_element_impl(selector: str, timeout: float, visible: bool) -> bool:
    page = _page()
    try:
        page.wait_for_selector(
            selector,
            timeout=timeout * 1000,
            state="visible" if visible else "attached",
        )
        return True
    except Exception:
        return False


def wait_for_element(selector: str, visible: bool = False, timeout: float = 10.0) -> bool:
    return _run(_wait_for_element_impl, selector, timeout, visible)


def _wait_for_network_idle_impl(timeout: float, idle_ms: float) -> bool:
    page = _page()
    try:
        page.wait_for_load_state("networkidle", timeout=timeout * 1000)
        return True
    except Exception:
        return False


def wait_for_network_idle(timeout: float = 10.0, idle_ms: float = 500) -> bool:
    return _run(_wait_for_network_idle_impl, timeout, idle_ms)


# --- tabs ---


def _list_tabs_impl(include_chrome: bool) -> list[dict]:
    context = _state["context"]
    if context is None:
        raise RuntimeError("playwright_helpers: not connected -- call connect() first")
    out = []
    for page in context.pages:
        url = page.url
        if not include_chrome and url.startswith(("chrome://", "chrome-untrusted://", "devtools://", "about:")):
            continue
        _tab_ids[id(page)] = page
        out.append({"targetId": str(id(page)), "target_id": str(id(page)), "title": page.title(), "url": url})
    return out


def list_tabs(include_chrome: bool = True) -> list[dict]:
    return _run(_list_tabs_impl, include_chrome)


def _switch_tab_impl(target_id: str) -> str:
    page = _tab_ids.get(int(target_id))
    if page is None:
        raise RuntimeError(f"switch_tab: unknown target_id {target_id!r}")
    page.bring_to_front()
    _state["page"] = page
    return target_id


def switch_tab(target_id: str) -> str:
    return _run(_switch_tab_impl, target_id)


def _upload_file_impl(selector: str, path: str) -> None:
    _page().set_input_files(selector, path)


def upload_file(selector: str, path: str) -> None:
    _run(_upload_file_impl, selector, path)


# --- visual ---


def _capture_screenshot_impl(path: str | None, full: bool, max_dim: int | None) -> str:
    page = _page()
    path = path or "shot.png"
    page.screenshot(path=path, full_page=full)
    if max_dim:
        from PIL import Image

        img = Image.open(path)
        if max(img.size) > max_dim:
            img.thumbnail((max_dim, max_dim))
            img.save(path)
    return path


def capture_screenshot(path: str | None = None, full: bool = False, max_dim: int | None = None) -> str:
    return _run(_capture_screenshot_impl, path, full, max_dim)
