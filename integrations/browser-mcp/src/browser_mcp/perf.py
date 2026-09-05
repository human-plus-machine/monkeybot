"""Opt-in per-tool timing and harness-call counting for browser-mcp.

Enabled with ``BROWSER_MCP_PERF=1``. Records one JSONL line per public tool
invocation: ``{ts, tool, wall_ms, harness_calls, result_chars, ok}``. Never
logs tool arguments (typed text and playbooks may contain PII).
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from browser_mcp import screenshots

_TRUE = frozenset({"1", "true", "yes"})
_tls = threading.local()


def enabled() -> bool:
    return os.environ.get("BROWSER_MCP_PERF", "").strip().lower() in _TRUE


def log_path() -> Path:
    raw = os.environ.get("BROWSER_MCP_PERF_LOG", "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
    return (screenshots.workspace_root() / "browser" / "perf" / "tools.jsonl").resolve()


def reset_harness_calls() -> None:
    _tls.calls = 0


def harness_call_count() -> int:
    return int(getattr(_tls, "calls", 0))


def _increment_harness_calls() -> None:
    _tls.calls = harness_call_count() + 1


class CountingHelpers:
    """Proxy that counts each callable attribute invocation on the helpers module."""

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def counted(*args: Any, **kwargs: Any) -> Any:
            _increment_harness_calls()
            return attr(*args, **kwargs)

        return counted

    def __dir__(self) -> list[str]:
        return dir(self._inner)


def wrap_helpers(helpers: Any) -> Any:
    if not enabled():
        return helpers
    if isinstance(helpers, CountingHelpers):
        return helpers
    return CountingHelpers(helpers)


def unwrap(helpers: Any) -> Any:
    if isinstance(helpers, CountingHelpers):
        return helpers._inner
    return helpers


class _ToolRecord:
    __slots__ = ("ok", "result_chars")

    def __init__(self) -> None:
        self.ok = True
        self.result_chars = 0

    def observe(self, result: str) -> None:
        self.result_chars = len(result)
        try:
            payload = json.loads(result)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if isinstance(payload, dict) and payload.get("ok") is False:
            self.ok = False

    def fail(self) -> None:
        self.ok = False


def _append_log(tool: str, rec: _ToolRecord, wall_ms: float, calls: int) -> None:
    line = {
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "tool": tool,
        "wall_ms": round(wall_ms, 3),
        "harness_calls": calls,
        "result_chars": rec.result_chars,
        "ok": rec.ok,
    }
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        return


@contextmanager
def timed_tool(tool: str) -> Iterator[_ToolRecord]:
    rec = _ToolRecord()
    if not enabled():
        yield rec
        return
    reset_harness_calls()
    started = time.perf_counter()
    try:
        yield rec
    except Exception:
        rec.fail()
        raise
    finally:
        if enabled():
            _append_log(
                tool,
                rec,
                (time.perf_counter() - started) * 1000.0,
                harness_call_count(),
            )
