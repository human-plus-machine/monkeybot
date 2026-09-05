"""JSON tool payloads and observe-mode constants."""

from __future__ import annotations

import json
from typing import Any

from browser_mcp import tabs

_ELEMENT_KINDS = frozenset({"inputs", "buttons", "links", "all"})
_OBSERVE_MODES = frozenset({"full", "diff"})
_ACTION_OBSERVE_MODES = frozenset({"full", "diff", "none"})
_VIEWPORT_OFF = frozenset({"0", "false", "no", "off"})
_VIEWPORT_ON = frozenset({"1", "true", "yes", "on"})
_DIFF_TO_FULL_RATIO = 0.6


def json_text(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def observe_error(value: object, allowed: frozenset[str]) -> str:
    names = ", ".join(sorted(allowed))
    return json_text({"ok": False, "error": f"unknown observe {value!r}; expected {names}"})


def with_observation(
    payload: dict[str, Any], wrapped: dict[str, Any] | None
) -> dict[str, Any]:
    if wrapped is None:
        return payload
    return {**payload, **wrapped}


def unknown_tab_result(exc: tabs.UnknownTabError) -> str:
    return json_text({"ok": False, "error": str(exc)})
