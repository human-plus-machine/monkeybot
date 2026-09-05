"""Per-tab registry, aliases, and session-routed handles for browser-mcp.

The daemon holds one focused CDP session. Reads can address any tab via a
cached ``session_id`` (or a Playwright page object) without calling
``switch_tab``. Actions focus first because headed Chrome throttles timers and
pauses rendering in background tabs.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
_CHROME_INTERNAL = ("chrome://", "chrome-untrusted://", "devtools://", "chrome-extension://")
_SESSION_LOST = "session with given id not found"
_SINGLE_TAB_MARKERS = (
    "not allowed",
    "not supported",
    "unknown method",
    "doesn't exist",
    "cannot create",
    "single tab",
)
_PAGE_INFO_JS = (
    "JSON.stringify({url:location.href,title:document.title,w:innerWidth,"
    "h:innerHeight,sx:scrollX,sy:scrollY,"
    "pw:document.documentElement.scrollWidth,ph:document.documentElement.scrollHeight})"
)
_READABLE_TEXT_JS = (
    "(() => {"
    "const root = document.querySelector('main, article, [role=main]') || document.body;"
    "if (!root) return '';"
    "const clone = root.cloneNode(true);"
    "clone.querySelectorAll('script,style,nav,footer,aside').forEach((e) => e.remove());"
    "return (clone.innerText || clone.textContent || '').replace(/\\s+/g, ' ').trim();"
    "})()"
)
_TAB_LIMIT_ACTION = (
    "Ask the user which tab to close (show them the list with aliases, titles, "
    "and last-used times), then call browser_close_tab(tab) with their choice "
    "and retry. Do not close a tab without the user's confirmation."
)


class UnknownTabError(LookupError):
    """Raised when ``tab`` is not an alias or target id in the registry."""


class TabLimitError(RuntimeError):
    """Opening another agent-controlled tab would exceed ``BROWSER_MCP_MAX_TABS``."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("error", "tab_limit_reached"))
        self.payload = payload


class SingleTabBackendError(RuntimeError):
    """The backend cannot create additional targets (in-app Spaces, etc.)."""

    def __init__(self, message: str = "this browser backend supports a single tab") -> None:
        super().__init__(message)


def max_tabs() -> int:
    raw = (os.environ.get("BROWSER_MCP_MAX_TABS") or "5").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 5
    return max(1, n)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _has_helper(helpers: Any, name: str) -> bool:
    return callable(getattr(helpers, name, None))


def _target_id_of(tab: dict[str, Any] | None) -> str | None:
    if not isinstance(tab, dict):
        return None
    tid = tab.get("targetId") or tab.get("target_id")
    return str(tid) if tid else None


def _is_chrome_internal(url: str) -> bool:
    return (url or "").startswith(_CHROME_INTERNAL)


def is_single_tab_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _SINGLE_TAB_MARKERS)


def _js_snippet(expression: str, limit: int = 160) -> str:
    snippet = expression.strip().replace("\n", "\\n")
    return snippet[: limit - 3] + "..." if len(snippet) > limit else snippet


def _js_exception_description(result: dict[str, Any], details: dict[str, Any] | None) -> str:
    desc = result.get("description")
    exc = details.get("exception") if details else None
    if not desc and isinstance(exc, dict):
        desc = exc.get("description")
        if desc is None and "value" in exc:
            desc = str(exc["value"])
        if desc is None:
            desc = exc.get("className")
    if not desc and details:
        desc = details.get("text")
    return desc or "JavaScript evaluation failed"


def _decode_unserializable_js_value(value: str) -> Any:
    if value == "NaN":
        return math.nan
    if value == "Infinity":
        return math.inf
    if value == "-Infinity":
        return -math.inf
    if value == "-0":
        return -0.0
    if value.endswith("n"):
        return int(value[:-1])
    return value


def runtime_value(response: dict[str, Any], expression: str) -> Any:
    """Decode a CDP ``Runtime.evaluate`` result dict (not a private harness helper)."""
    result = response.get("result", {}) if isinstance(response, dict) else {}
    details = response.get("exceptionDetails") if isinstance(response, dict) else None
    if not isinstance(result, dict):
        result = {}
    if details or result.get("subtype") == "error":
        desc = _js_exception_description(result, details if isinstance(details, dict) else None)
        loc = ""
        if isinstance(details, dict):
            line = details.get("lineNumber")
            col = details.get("columnNumber")
            loc = (
                f" at line {line}, column {col}"
                if line is not None and col is not None
                else ""
            )
        raise RuntimeError(
            f"JavaScript evaluation failed{loc}: {desc}; expression: {_js_snippet(expression)}"
        )
    if "value" in result:
        return result["value"]
    if "unserializableValue" in result:
        return _decode_unserializable_js_value(str(result["unserializableValue"]))
    return None


def cdp_evaluate(helpers: Any, expression: str, session_id: str | None, await_promise: bool) -> Any:
    response = helpers.cdp(
        "Runtime.evaluate",
        session_id=session_id,
        expression=expression,
        awaitPromise=await_promise,
        returnByValue=True,
    )
    return runtime_value(response if isinstance(response, dict) else {}, expression)


@dataclass
class TabState:
    target_id: str
    tab: str
    alias: str
    session_id: str | None = None
    driver_registered: bool = False
    last_tree: list[str] | None = None
    last_url: str | None = None
    opened_by_agent: bool = False
    touched_by_agent: bool = False
    last_used: str | None = None
    url: str = ""
    title: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def agent_controlled(self) -> bool:
        return self.opened_by_agent or self.touched_by_agent


class TabHandle:
    """Evaluate / navigate against one tab. Focused handles use ``helpers.js`` as today."""

    def __init__(self, helpers: Any, state: TabState | None, *, focused: bool) -> None:
        self.helpers = helpers
        self.state = state
        self.focused = focused
        self.switch_session_id: str | None = None

    @property
    def target_id(self) -> str | None:
        return self.state.target_id if self.state else None

    def evaluate(self, expression: str, await_promise: bool = True) -> Any:
        if self.focused or self.state is None:
            return self.helpers.js(expression)
        if _has_helper(self.helpers, "cdp"):
            return self._cdp_evaluate(expression, await_promise)
        if self.state is not None:
            return self.helpers.js(expression, target_id=self.state.target_id)
        return self.helpers.js(expression)

    def _cdp_evaluate(self, expression: str, await_promise: bool) -> Any:
        assert self.state is not None
        sid = registry().session_for(self.helpers, self.state)
        try:
            return cdp_evaluate(self.helpers, expression, sid, await_promise)
        except RuntimeError as exc:
            if _SESSION_LOST not in str(exc).lower():
                raise
            self.state.session_id = None
            sid = registry().session_for(self.helpers, self.state)
            return cdp_evaluate(self.helpers, expression, sid, await_promise)

    def cdp(self, method: str, **params: Any) -> Any:
        if not _has_helper(self.helpers, "cdp"):
            raise AttributeError("cdp")
        if self.focused or self.state is None:
            return self.helpers.cdp(method, **params)
        sid = registry().session_for(self.helpers, self.state)
        try:
            return self.helpers.cdp(method, session_id=sid, **params)
        except RuntimeError as exc:
            if _SESSION_LOST not in str(exc).lower():
                raise
            self.state.session_id = None
            sid = registry().session_for(self.helpers, self.state)
            return self.helpers.cdp(method, session_id=sid, **params)

    def navigate(self, url: str) -> Any:
        if self.focused or self.state is None:
            return self.helpers.goto_url(url)
        if _has_helper(self.helpers, "cdp"):
            return self.cdp("Page.navigate", url=url)
        return self.helpers.goto_url(url, target_id=self.state.target_id)

    def page_info(self) -> dict[str, Any]:
        if self.focused or self.state is None:
            info = self.helpers.page_info()
            return info if isinstance(info, dict) else {}
        raw = self.evaluate(_PAGE_INFO_JS)
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def capture_screenshot(
        self, path: str | None = None, full: bool = False, max_dim: int | None = None
    ) -> str:
        if self.focused or self.state is None:
            return self.helpers.capture_screenshot(path=path, full=full, max_dim=max_dim)
        if _has_helper(self.helpers, "capture_screenshot"):
            try:
                return self.helpers.capture_screenshot(
                    path=path, full=full, max_dim=max_dim, target_id=self.state.target_id
                )
            except TypeError:
                pass
        return self.helpers.capture_screenshot(path=path, full=full, max_dim=max_dim)

    def readable_text(self) -> str:
        result = self.evaluate(_READABLE_TEXT_JS)
        return str(result or "")


class TabRegistry:
    def __init__(self) -> None:
        self._tabs: dict[str, TabState] = {}
        self._aliases: dict[str, str] = {}
        self._next_n: int = 1
        self._retired: set[str] = set()
        self._focused_id: str | None = None
        self.init_script_registered: bool = False

    def reset(self) -> None:
        self._tabs.clear()
        self._aliases.clear()
        self._next_n = 1
        self._retired.clear()
        self._focused_id = None
        self.init_script_registered = False

    @property
    def focused_id(self) -> str | None:
        return self._focused_id

    def set_focused(self, target_id: str | None) -> None:
        self._focused_id = target_id

    def tabs(self) -> list[TabState]:
        return list(self._tabs.values())

    def get(self, target_id: str) -> TabState | None:
        return self._tabs.get(target_id)

    def focused(self) -> TabState | None:
        if self._focused_id and self._focused_id in self._tabs:
            return self._tabs[self._focused_id]
        return None

    def _alloc_tn(self) -> str:
        while True:
            name = f"t{self._next_n}"
            self._next_n += 1
            if name not in self._retired and name not in self._aliases:
                return name

    def _bind_alias(self, alias: str, target_id: str) -> None:
        self._aliases[alias] = target_id

    def _drop_state(self, state: TabState, helpers: Any | None) -> None:
        self._tabs.pop(state.target_id, None)
        stale = [name for name, tid in self._aliases.items() if tid == state.target_id]
        for name in stale:
            self._aliases.pop(name, None)
            self._retired.add(name)
        if state.session_id and helpers is not None and _has_helper(helpers, "cdp"):
            try:
                helpers.cdp("Target.detachFromTarget", sessionId=state.session_id)
            except Exception:
                pass
        if self._focused_id == state.target_id:
            self._focused_id = None

    def refresh(self, helpers: Any) -> None:
        if not _has_helper(helpers, "list_tabs"):
            self._sync_focus(helpers)
            return
        raw = helpers.list_tabs(include_chrome=True)
        if not isinstance(raw, list):
            self._sync_focus(helpers)
            return
        live: dict[str, dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if _is_chrome_internal(url):
                continue
            tid = _target_id_of(item)
            if not tid:
                continue
            live[tid] = item
        for tid in list(self._tabs):
            if tid not in live:
                self._drop_state(self._tabs[tid], helpers)
        for tid, item in live.items():
            url = str(item.get("url") or "")
            title = str(item.get("title") or "")
            if tid in self._tabs:
                state = self._tabs[tid]
                state.url = url
                state.title = title
                continue
            tab = self._alloc_tn()
            state = TabState(
                target_id=tid,
                tab=tab,
                alias=tab,
                url=url,
                title=title,
            )
            self._tabs[tid] = state
            self._bind_alias(tab, tid)
        self._sync_focus(helpers)

    def _sync_focus(self, helpers: Any) -> None:
        if not _has_helper(helpers, "current_tab"):
            return
        try:
            current = helpers.current_tab()
        except Exception:
            return
        tid = _target_id_of(current if isinstance(current, dict) else None)
        if tid:
            self._focused_id = tid
            if tid not in self._tabs and not _is_chrome_internal(str((current or {}).get("url") or "")):
                tab = self._alloc_tn()
                url = str((current or {}).get("url") or "")
                title = str((current or {}).get("title") or "")
                state = TabState(target_id=tid, tab=tab, alias=tab, url=url, title=title)
                self._tabs[tid] = state
                self._bind_alias(tab, tid)

    def resolve(self, tab: str | None) -> TabState:
        if tab is None or tab == "":
            focused = self.focused()
            if focused is not None:
                return focused
            raise UnknownTabError("unknown tab; no focused tab")
        if tab in self._aliases:
            return self._tabs[self._aliases[tab]]
        if tab in self._tabs:
            return self._tabs[tab]
        raise UnknownTabError(self.unknown_message(tab))

    def unknown_message(self, tab: str) -> str:
        known = self._known_labels()
        suffix = f"; known: {known}" if known else "; known: (none)"
        return f"unknown tab {tab!r}{suffix}"

    def _known_labels(self) -> str:
        labels: list[str] = []
        for state in self.sorted_tabs():
            if state.target_id == self._focused_id:
                note = "focused"
            elif state.alias != state.tab:
                note = state.alias
            else:
                note = ""
            labels.append(f"{state.tab} ({note})" if note else state.tab)
        return ", ".join(labels)

    def sorted_tabs(self) -> list[TabState]:
        items = list(self._tabs.values())
        items.sort(key=lambda s: (0 if s.target_id == self._focused_id else 1, s.tab))
        return items

    def set_alias(self, tab: str, alias: str) -> TabState:
        state = self.resolve(tab)
        if not _ALIAS_RE.match(alias):
            raise ValueError(
                f"alias {alias!r} must match [a-z][a-z0-9_-]{{0,23}}"
            )
        owner = self._aliases.get(alias)
        if owner is not None and owner != state.target_id:
            raise ValueError(f"alias {alias!r} is already used")
        if state.alias != state.tab:
            self._aliases.pop(state.alias, None)
        state.alias = alias
        self._bind_alias(alias, state.target_id)
        return state

    def session_for(self, helpers: Any, state: TabState) -> str:
        if state.session_id:
            return state.session_id
        result = helpers.cdp("Target.attachToTarget", targetId=state.target_id, flatten=True)
        if not isinstance(result, dict) or not result.get("sessionId"):
            raise RuntimeError(f"attachToTarget failed for {state.target_id}")
        sid = str(result["sessionId"])
        helpers.cdp("Runtime.enable", session_id=sid)
        helpers.cdp("Page.enable", session_id=sid)
        state.session_id = sid
        return sid

    def mark_used(self, state: TabState) -> None:
        state.touched_by_agent = True
        state.last_used = utc_now()

    def agent_controlled(self) -> list[TabState]:
        return [s for s in self._tabs.values() if s.agent_controlled]

    def agent_opened(self) -> list[TabState]:
        return [s for s in self._tabs.values() if s.opened_by_agent]

    def would_exceed_cap(self, adding: int = 1) -> bool:
        return len(self.agent_controlled()) + adding > max_tabs()

    def cap_error_payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "tab_limit_reached",
            "limit": max_tabs(),
            "tabs": [self._public_entry(s) for s in self.sorted_tabs() if s.agent_controlled],
            "action_required": _TAB_LIMIT_ACTION,
        }

    def _public_entry(self, state: TabState) -> dict[str, Any]:
        return {
            "tab": state.tab,
            "alias": state.alias,
            "url": state.url,
            "title": state.title,
            "focused": state.target_id == self._focused_id,
            "opened_by_agent": state.opened_by_agent,
            "last_used": state.last_used,
        }

    def list_payload(self) -> dict[str, Any]:
        focused = self.focused()
        return {
            "ok": True,
            "focused": focused.tab if focused else None,
            "tabs": [self._public_entry(s) for s in self.sorted_tabs()],
        }

    def handle(self, helpers: Any, state: TabState, *, focused: bool) -> TabHandle:
        return TabHandle(helpers, state, focused=focused)

    def focused_handle(self, helpers: Any, state: TabState | None = None) -> TabHandle:
        return TabHandle(helpers, state if state is not None else self.focused(), focused=True)

    def most_recently_used(self, excluding: str | None = None) -> TabState | None:
        candidates = [s for s in self._tabs.values() if s.target_id != excluding]
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.last_used or "", reverse=True)
        return candidates[0]

    def detach_all(self, helpers: Any) -> None:
        if not _has_helper(helpers, "cdp"):
            self.reset()
            return
        for state in list(self._tabs.values()):
            if state.session_id:
                try:
                    helpers.cdp("Target.detachFromTarget", sessionId=state.session_id)
                except Exception:
                    pass
        self.reset()

    def remember_created(
        self, helpers: Any, *, opened_by_agent: bool, alias: str | None = None
    ) -> TabState:
        """Refresh and return the newest target, marking it agent-opened if asked."""
        before = set(self._tabs)
        self.refresh(helpers)
        created = [s for tid, s in self._tabs.items() if tid not in before]
        if not created:
            focused = self.focused()
            if focused is None:
                raise RuntimeError("failed to register new tab")
            state = focused
        else:
            state = created[-1]
        if opened_by_agent:
            state.opened_by_agent = True
        self.mark_used(state)
        if alias:
            self.set_alias(state.tab, alias)
        return state


_registry = TabRegistry()


def registry() -> TabRegistry:
    return _registry


def reset_registry() -> None:
    _registry.reset()


def as_handle(target: Any) -> TabHandle:
    if isinstance(target, TabHandle):
        return target
    return TabHandle(target, None, focused=True)
