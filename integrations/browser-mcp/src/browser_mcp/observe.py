"""Settle, snapshot, and post-action observation."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from browser_mcp import actions, dom_indexing, results, tabs

logger = logging.getLogger(__name__)

_LOAD_WAIT_JS = actions.LOAD_WAIT_JS
_NETWORK_STARTED = "Network.requestWillBeSent"
_NETWORK_ENDED = frozenset(
    {
        "Network.loadingFinished",
        "Network.loadingFailed",
        "Network.loadingCancelled",
    }
)


def _resolve_viewport_only(value: bool | None) -> bool:
    if value is not None:
        return bool(value)
    raw = (os.environ.get("BROWSER_MCP_VIEWPORT_DEFAULT") or "1").strip().lower()
    if raw in results.VIEWPORT_OFF:
        return False
    if raw in results.VIEWPORT_ON:
        return True
    return True


def _cache_tree(handle: tabs.TabHandle, url: str | None, lines: list[str]) -> None:
    state = handle.state
    if state is None:
        logger.debug(
            "cache_tree skipped: handle has no TabState (tree_n=%s url=%s)",
            len(lines),
            url,
        )
        return
    state.last_tree = list(lines)
    if url:
        state.last_url = url
    logger.debug(
        "cache_tree target=%s tree_n=%s url=%s",
        state.target_id,
        len(state.last_tree),
        state.last_url,
    )


def _env_ms(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _quiet_ms() -> int:
    return _env_ms("BROWSER_MCP_QUIET_MS", 150)


def _settle_ms() -> int:
    return _env_ms("BROWSER_MCP_SETTLE_MS", 1500)


def resolve_action_observe(value: str | None, *, default: str = "diff") -> str:
    if value is not None and str(value).strip():
        return str(value).strip().lower()
    if default != "diff":
        return default
    env = (os.environ.get("BROWSER_MCP_OBSERVE_DEFAULT") or "diff").strip().lower()
    return env if env in results.ACTION_OBSERVE_MODES else "diff"


def _remaining_ms(started: float, budget_ms: int) -> int:
    used = int((time.monotonic() - started) * 1000)
    return max(0, budget_ms - used)


def _network_in_flight(helpers: Any, in_flight: set[str]) -> bool:
    drain = getattr(helpers, "drain_events", None)
    if not callable(drain):
        return bool(in_flight)
    try:
        events = drain()
    except Exception:
        logger.debug("drain_events failed", exc_info=True)
        return bool(in_flight)
    if not isinstance(events, (list, tuple)):
        return bool(in_flight)
    for event in events:
        if not isinstance(event, dict):
            continue
        method = str(event.get("method") or "")
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        req_id = params.get("requestId")
        if not req_id:
            continue
        key = str(req_id)
        if method == _NETWORK_STARTED:
            in_flight.add(key)
        elif method in _NETWORK_ENDED:
            in_flight.discard(key)
    return bool(in_flight)


def _wait_while_network_busy(
    handle: tabs.TabHandle, *, remaining_ms: int, quiet_ms: int
) -> dict[str, Any]:
    helpers = handle.helpers
    in_flight: set[str] = set()
    if _network_in_flight(helpers, in_flight):
        extra = {"quiet": True, "navigated": False}
        deadline = time.monotonic() + remaining_ms / 1000.0
        while in_flight and time.monotonic() < deadline:
            left = max(0, int((deadline - time.monotonic()) * 1000))
            chunk = min(max(quiet_ms, 50), left) if left else 0
            if chunk <= 0:
                break
            extra = dom_indexing.settle(handle, quiet_ms=min(50, chunk), max_ms=chunk)
            if extra.get("navigated"):
                return extra
            _network_in_flight(helpers, in_flight)
        return extra
    drain = getattr(helpers, "drain_events", None)
    wait_idle = getattr(helpers, "wait_for_network_idle", None)
    if callable(drain) or not callable(wait_idle) or remaining_ms <= 0:
        return {"quiet": True, "navigated": False}
    try:
        wait_idle(timeout=remaining_ms / 1000.0, idle_ms=float(max(quiet_ms, 1)))
    except TypeError:
        wait_idle(timeout=remaining_ms / 1000.0)
    except Exception:
        logger.debug("wait_for_network_idle failed", exc_info=True)
    return {"quiet": True, "navigated": False}


def _handle_navigated(handle: tabs.TabHandle) -> None:
    try:
        handle.evaluate(_LOAD_WAIT_JS)
    except Exception:
        logger.debug("post-navigation load wait failed", exc_info=True)
    dom_indexing._register_driver_for_new_documents(handle)


def _settle_post_action(
    handle: tabs.TabHandle, *, quiet_ms: int, max_ms: int
) -> dict[str, Any]:
    started = time.monotonic()
    settled = dom_indexing.settle(handle, quiet_ms=quiet_ms, max_ms=max_ms)
    logger.debug(
        "observe settle js quiet_ms=%s max_ms=%s elapsed_ms=%.0f result=%s",
        quiet_ms,
        max_ms,
        (time.monotonic() - started) * 1000,
        settled,
    )
    if settled.get("navigated"):
        _handle_navigated(handle)
        return {"quiet": True, "navigated": True, "mutations": settled.get("mutations", 0)}
    leftover = _remaining_ms(started, max_ms)
    net = _wait_while_network_busy(handle, remaining_ms=leftover, quiet_ms=quiet_ms)
    logger.debug("observe network leftover_ms=%s result=%s", leftover, net)
    if net.get("navigated"):
        _handle_navigated(handle)
        return {"quiet": True, "navigated": True, "mutations": settled.get("mutations", 0)}
    if not net.get("quiet", True):
        settled = {**settled, "quiet": False}
    return settled


def _url_without_fragment(url: str) -> str:
    return (url or "").split("#", 1)[0]


def _page_navigated(handle: tabs.TabHandle, url: str) -> bool:
    return bool(
        handle.state
        and handle.state.last_url
        and url
        and _url_without_fragment(handle.state.last_url) != _url_without_fragment(url)
    )


def _full_observation(
    *,
    url: str,
    title: str,
    element_count: int,
    tree: str,
    truncated: bool,
    below_viewport: int,
) -> dict[str, Any]:
    return {
        "mode": "full",
        "url": url,
        "title": title,
        "elementCount": element_count,
        "tree": tree,
        "truncated": truncated,
        "below_viewport": below_viewport,
    }


def _diff_observation(
    previous: list[str],
    lines: list[str],
    *,
    url: str,
    title: str,
    element_count: int,
    truncated: bool,
    below_viewport: int,
) -> dict[str, Any] | None:
    diff = dom_indexing.diff_tree_lines(previous, lines)
    added = list(diff["added"])
    removed = list(diff["removed"])
    oversized = (
        len(lines) >= 8
        and len(added) + len(removed) > results.DIFF_TO_FULL_RATIO * len(lines)
    )
    if oversized:
        return None
    return {
        "mode": "diff",
        "added": added,
        "removed": removed,
        "unchanged": diff["unchanged"],
        "elementCount": element_count,
        "url": url,
        "title": title,
        "truncated": truncated,
        "below_viewport": below_viewport,
    }


def snapshot_tree(
    handle: tabs.TabHandle,
    observe: str,
    *,
    viewport_only: bool | None = None,
    kind: str | None = None,
    contains: str | None = None,
    max_elements: int = 150,
) -> dict[str, Any]:
    viewport = _resolve_viewport_only(viewport_only)
    result = dom_indexing.get_elements(
        handle,
        viewport,
        kind=kind,
        contains=contains,
        max_elements=max_elements,
    )
    if result.get("error"):
        return {"error": result["error"], "observation": {"mode": "full", "tree": ""}, "_lines": []}
    raw_tree = str(result.get("tree") or "")
    lines = dom_indexing.tree_lines(raw_tree)
    truncated = bool(result.get("truncated"))
    below_viewport = int(result.get("below_viewport") or 0)
    omitted = int(result.get("omitted") or 0)
    url = str(result.get("url") or "")
    title = str(result.get("title") or "")
    element_count = int(result.get("elementCount") or 0)
    searching = bool(contains and str(contains).strip())
    tree = dom_indexing.attach_tree_footers(
        raw_tree,
        viewport_only=viewport and not searching,
        below_viewport=below_viewport,
        truncated=truncated,
        omitted=omitted,
    )
    navigated = _page_navigated(handle, url)
    previous = handle.state.last_tree if handle.state is not None else None
    if observe == "diff" and previous is not None and not navigated:
        diff_obs = _diff_observation(
            previous,
            lines,
            url=url,
            title=title,
            element_count=element_count,
            truncated=truncated,
            below_viewport=below_viewport,
        )
        if diff_obs is not None:
            _cache_tree(handle, url, lines)
            return {
                "url": url,
                "title": title,
                "navigated": navigated,
                "observation": diff_obs,
                "_lines": lines,
            }
    _cache_tree(handle, url, lines)
    return {
        "url": url,
        "title": title,
        "navigated": navigated,
        "observation": _full_observation(
            url=url,
            title=title,
            element_count=element_count,
            tree=tree,
            truncated=truncated,
            below_viewport=below_viewport,
        ),
        "_lines": lines,
    }


def _retry_until_tree_changes(
    handle: tabs.TabHandle,
    observe: str,
    *,
    before_lines: list[str],
    started: float,
    budget: int,
    quiet: int,
    settled: dict[str, Any],
    snap: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    retries = 0
    navigated = bool(settled.get("navigated")) or bool(snap.get("navigated"))
    while _remaining_ms(started, budget) > 0:
        if list(snap.get("_lines") or []) != before_lines:
            logger.debug("observe_after retry stop: tree changed after %s extra settles", retries)
            break
        left = _remaining_ms(started, budget)
        logger.debug("observe_after retry n=%s left_ms=%s", retries, left)
        tick = time.monotonic()
        extra = _settle_post_action(
            handle, quiet_ms=min(quiet, left) if quiet else left, max_ms=left
        )
        if (time.monotonic() - tick) * 1000 < 20 and left > 0:
            time.sleep(min(max(quiet, 1), left) / 1000.0)
        retries += 1
        if extra.get("navigated"):
            snap = snapshot_tree(handle, observe)
            logger.debug("observe_after retry navigated extra=%s url=%s", extra, snap.get("url"))
            return extra, snap, True
        snap = snapshot_tree(handle, observe)
        logger.debug(
            "observe_after retry snap n=%s nav=%s mode=%s url=%s changed=%s tree=%r",
            retries,
            snap.get("navigated"),
            (snap.get("observation") or {}).get("mode"),
            snap.get("url"),
            list(snap.get("_lines") or []) != before_lines,
            "\n".join(snap.get("_lines") or [])[:500],
        )
        if snap.get("navigated"):
            return extra, snap, True
        settled = extra
    return settled, snap, navigated


def observe_after(
    handle: tabs.TabHandle,
    observe: str,
    action: dict[str, Any],
    *,
    before_url: str,
    retry_until_change: bool = False,
) -> dict[str, Any] | None:
    if observe == "none":
        return None
    quiet = _quiet_ms()
    budget = _settle_ms()
    started = time.monotonic()
    before_lines = (
        list(handle.state.last_tree) if handle.state and handle.state.last_tree else None
    )
    logger.debug(
        "observe_after start action=%s observe=%s retry=%s quiet_ms=%s settle_ms=%s "
        "before_url=%s last_tree_n=%s last_url=%s",
        action.get("type"),
        observe,
        retry_until_change,
        quiet,
        budget,
        before_url,
        None if before_lines is None else len(before_lines),
        handle.state.last_url if handle.state else None,
    )
    if retry_until_change and before_lines is None:
        logger.debug(
            "observe_after retry skipped: no last_tree (state=%s)",
            None if handle.state is None else handle.state.target_id,
        )
    settled = _settle_post_action(handle, quiet_ms=quiet, max_ms=budget)
    snap = snapshot_tree(handle, observe)
    navigated = bool(settled.get("navigated")) or bool(snap.get("navigated"))
    retries = 0
    if retry_until_change and before_lines is not None and not navigated:
        settled, snap, navigated = _retry_until_tree_changes(
            handle,
            observe,
            before_lines=before_lines,
            started=started,
            budget=budget,
            quiet=quiet,
            settled=settled,
            snap=snap,
        )
        retries = 1
    if before_url and snap.get("url"):
        if _url_without_fragment(before_url) != _url_without_fragment(str(snap["url"])):
            navigated = True
            logger.debug(
                "observe_after url changed before=%s after=%s", before_url, snap.get("url")
            )
    observation = dict(snap.get("observation") or {"mode": "full", "tree": ""})
    quiet_flag = bool(settled.get("quiet", True))
    if not quiet_flag:
        observation["settled"] = False
    logger.debug(
        "observe_after done elapsed_ms=%.0f retries=%s navigated=%s settled=%s mode=%s",
        (time.monotonic() - started) * 1000,
        retries,
        navigated,
        quiet_flag,
        observation.get("mode"),
    )
    return {
        "action": action,
        "page": {
            "url": str(snap.get("url") or ""),
            "title": str(snap.get("title") or ""),
            "navigated": navigated,
            "settled": quiet_flag,
        },
        "observation": observation,
    }

