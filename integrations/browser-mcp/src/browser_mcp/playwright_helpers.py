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
Every public function below is a thin ``@_marshalled`` wrapper: the function
body runs on the executor thread, ``_run`` submits it and blocks for the result.
"""

from __future__ import annotations

import contextlib
import functools
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

_CONNECT_TIMEOUT_MS = 30_000

_executor: ThreadPoolExecutor | None = None

# Mutated only from inside the dedicated executor thread.
_state: dict[str, Any] = {"playwright": None, "browser": None, "context": None, "page": None}
_tab_ids: dict[int, Any] = {}  # synthesized target_id (id(page)) -> Page

# Set by server.py's AgentCore dispatch to (ws_url, headers) fresh-session
# credentials. Used to transparently reconnect once when a call fails because
# the ~15-minute AgentCore session expired out from under a live connection.
_reconnect_hook: Callable[[], tuple[str, dict[str, str]]] | None = None


def set_reconnect_hook(hook: Callable[[], tuple[str, dict[str, str]]] | None) -> None:
    global _reconnect_hook
    _reconnect_hook = hook


def _ensure_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agentcore-playwright")
    return _executor


def _is_connection_error(exc: BaseException) -> bool:
    """True for Playwright errors indicating the CDP connection itself died
    (stale/expired AgentCore session, network drop) -- as opposed to an
    ordinary page-level failure (element not found, JS exception, etc.)."""
    try:
        from playwright.sync_api import Error as PlaywrightError
    except ImportError:
        return False
    if not isinstance(exc, PlaywrightError):
        return False
    msg = str(exc).lower()
    return "closed" in msg or "disconnected" in msg


def _run(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return _ensure_executor().submit(fn, *args, **kwargs).result()
    except Exception as exc:
        if not _is_connection_error(exc) or _reconnect_hook is None:
            raise
        ws_url, headers = _reconnect_hook()
        _ensure_executor().submit(_connect_impl, ws_url, headers).result()
        return _ensure_executor().submit(fn, *args, **kwargs).result()


def _marshalled(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Submit fn's execution onto the dedicated Playwright thread (with the
    connection-retry behavior in _run), preserving fn's signature/docstring."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return _run(fn, *args, **kwargs)

    return wrapper


def _connect_impl(ws_url: str, headers: dict[str, str]) -> None:
    from playwright.sync_api import sync_playwright

    if _state["browser"] is not None:
        _disconnect_impl()

    pw = sync_playwright().start()
    # Playwright's driver rejects an empty headers dict ("expected array, got
    # object") -- only pass it through when non-empty. generate_ws_headers()
    # always returns a populated dict in practice (Host/X-Amz-Date/Authorization/...).
    browser = pw.chromium.connect_over_cdp(ws_url, headers=headers or None, timeout=_CONNECT_TIMEOUT_MS)
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
    """Close the Playwright browser/connection and reset state. Idempotent.

    Also shuts down the dedicated executor thread and clears the reconnect
    hook, so a later connect() starts a genuinely fresh thread rather than
    reusing one whose owning session is gone.
    """
    global _executor
    _run(_disconnect_impl)
    set_reconnect_hook(None)
    executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=False)


def _page() -> Any:
    page = _state["page"]
    if page is None:
        raise RuntimeError("playwright_helpers: not connected -- call connect() first")
    return page


# --- navigation / page ---


@_marshalled
def new_tab(url: str = "about:blank") -> str:
    """Navigate the current tab if it's still blank, else open a new one --
    mirrors browser_harness.helpers.new_tab's reuse-if-blank semantics, so
    repeated browser_goto calls don't pile up tabs from a fresh session but
    do open a new tab when the current one already has real content."""
    context = _state["context"]
    if context is None:
        raise RuntimeError("playwright_helpers: not connected -- call connect() first")
    page = _state["page"]
    blank = page is None or page.url in ("", "about:blank") or page.url.startswith("about:blank#")
    if page is None or (url != "about:blank" and not blank):
        page = context.new_page()
        _state["page"] = page
        _tab_ids[id(page)] = page
    if url and url != "about:blank":
        page.goto(url)
    return page.url


@_marshalled
def wait_for_load() -> bool:
    try:
        _page().wait_for_load_state("load")
        return True
    except Exception:
        return False


@_marshalled
def page_info() -> dict:
    page = _page()
    expression = (
        "JSON.stringify({url:location.href,title:document.title,w:innerWidth,"
        "h:innerHeight,sx:scrollX,sy:scrollY,"
        "pw:document.documentElement.scrollWidth,ph:document.documentElement.scrollHeight})"
    )
    return json.loads(page.evaluate(expression))


# --- input ---


@_marshalled
def click_at_xy(x: float, y: float, button: str = "left", clicks: int = 1) -> None:
    _page().mouse.click(x, y, button=button, click_count=clicks)


def _wait_for_element(selector: str, timeout: float, visible: bool) -> bool:
    page = _page()
    try:
        page.wait_for_selector(selector, timeout=timeout * 1000, state="visible" if visible else "attached")
        return True
    except Exception:
        return False


@_marshalled
def fill_input(selector: str, text: str, clear_first: bool = True, timeout: float = 0.0) -> None:
    page = _page()
    if timeout > 0 and not _wait_for_element(selector, timeout, False):
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


_MODIFIER_NAMES = {1: "Alt", 2: "Control", 4: "Meta", 8: "Shift"}


def _key_combo(key: str, modifiers: int) -> str:
    parts = [name for bit, name in _MODIFIER_NAMES.items() if modifiers & bit]
    parts.append(key)
    return "+".join(parts)


@_marshalled
def press_key(key: str, modifiers: int = 0) -> None:
    _page().keyboard.press(_key_combo(key, modifiers))


@_marshalled
def scroll(x: float, y: float, dy: float = -300, dx: float = 0) -> None:
    page = _page()
    page.mouse.move(x, y)
    page.mouse.wheel(dx, dy)


@_marshalled
def js(expression: str) -> Any:
    from playwright.sync_api import Error as PlaywrightError

    try:
        return _page().evaluate(expression)
    except PlaywrightError as exc:
        # Re-raise as RuntimeError so dom_indexing.py's existing
        # `except RuntimeError` message-sniffing keeps working unchanged.
        raise RuntimeError(str(exc)) from exc


@_marshalled
def wait_for_element(selector: str, visible: bool = False, timeout: float = 10.0) -> bool:
    return _wait_for_element(selector, timeout, visible)


@_marshalled
def wait_for_network_idle(timeout: float = 10.0, idle_ms: float = 500) -> bool:
    """idle_ms is accepted for interface parity with browser_harness.helpers
    but not applied -- Playwright's "networkidle" load state owns its own
    (~500ms) no-network-for-N window internally and doesn't expose a knob."""
    page = _page()
    try:
        page.wait_for_load_state("networkidle", timeout=timeout * 1000)
        return True
    except Exception:
        return False


# --- tabs ---


@_marshalled
def list_tabs(include_chrome: bool = True) -> list[dict]:
    context = _state["context"]
    if context is None:
        raise RuntimeError("playwright_helpers: not connected -- call connect() first")
    # Rebuilt from scratch each call so closed tabs are pruned rather than
    # accumulating forever.
    _tab_ids.clear()
    out = []
    for page in context.pages:
        url = page.url
        if not include_chrome and url.startswith(("chrome://", "chrome-untrusted://", "devtools://", "about:")):
            continue
        _tab_ids[id(page)] = page
        out.append({"targetId": str(id(page)), "target_id": str(id(page)), "title": page.title(), "url": url})
    return out


@_marshalled
def switch_tab(target_id: str) -> str:
    try:
        key = int(target_id)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"switch_tab: unknown target_id {target_id!r}") from exc
    page = _tab_ids.get(key)
    if page is None:
        raise RuntimeError(f"switch_tab: unknown target_id {target_id!r}")
    page.bring_to_front()
    _state["page"] = page
    return target_id


@_marshalled
def upload_file(selector: str, path: str) -> None:
    _page().set_input_files(selector, path)


# --- visual ---


@_marshalled
def capture_screenshot(path: str | None = None, full: bool = False, max_dim: int | None = None) -> str:
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
