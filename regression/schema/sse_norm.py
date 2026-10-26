"""Normalize a gateway SSE stream before a golden byte compare (§5.5.3).

Durations, trace ids, timestamps, and generated ids become fixed tokens.
Ping comments are heartbeats and are dropped. The gateway temp root is a
generated id; token estimates that count that path are set to 0 so the
golden does not depend on the host temp directory.
"""

from __future__ import annotations

import contextvars
import json
import re
from typing import Any

from testkit.sse import SseParser

update_golden: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "sse_update_golden",
    default=False,
)

_ZERO_KEYS = frozenset({"duration_ms", "estimated_tokens", "estimated_prompt_tokens"})
_AGENT_ROOT = re.compile(
    r"(?:/private)?/var/folders/\S*?/mb-gateway-[A-Za-z0-9_]+"
    r"|/tmp/mb-gateway-[A-Za-z0-9_]+"
    r"|/var/tmp/mb-gateway-[A-Za-z0-9_]+"
)
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\d{4}-\d{2}-\d{2}"
)
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def normalize_sse(text: str) -> str:
    """Return framed bytes with §5.5.3 tokens substituted. Idempotent."""
    parser = SseParser()
    frames = parser.feed(text.encode("utf-8"))
    frames.extend(parser.finish())
    seen: list[str] = []
    parts: list[str] = []
    for frame in frames:
        if frame.data is None:
            continue
        data = _normalize_data(frame.data, seen)
        lines: list[str] = []
        if frame.id is not None:
            lines.append(f"id: {frame.id}")
        lines.append(f"data: {data}")
        parts.append("\n".join(lines) + "\n\n")
    return "".join(parts)


def _normalize_data(data: str, seen: list[str]) -> str:
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return _strings(data, seen)
    if not isinstance(parsed, dict):
        return json.dumps(_walk(parsed, seen), ensure_ascii=False, separators=(",", ":"))
    return json.dumps(_walk(parsed, seen), ensure_ascii=False, separators=(",", ":"))


def _walk(value: Any, seen: list[str], key: str | None = None) -> Any:
    if key in _ZERO_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
        return 0
    if key == "trace_id" and isinstance(value, str):
        return "trace"
    if isinstance(value, str):
        return _strings(value, seen)
    if isinstance(value, list):
        return [_walk(item, seen) for item in value]
    if isinstance(value, dict):
        return {name: _walk(item, seen, name) for name, item in value.items()}
    return value


def _strings(text: str, seen: list[str]) -> str:
    text = _AGENT_ROOT.sub("/agent", text)

    def stamp(match: re.Match[str]) -> str:
        if "T" in match.group(0):
            return "1970-01-01T00:00:00Z"
        return "1970-01-01"

    text = _TIMESTAMP.sub(stamp, text)

    def uuid(match: re.Match[str]) -> str:
        token = match.group(0).lower()
        if token not in seen:
            seen.append(token)
        return f"id-{seen.index(token) + 1}"

    return _UUID.sub(uuid, text)


__all__ = ["normalize_sse", "update_golden"]
