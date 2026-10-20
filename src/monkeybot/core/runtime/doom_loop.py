"""Doom-loop detection: consecutive identical tool calls within one user message."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from collections.abc import Sequence
from typing import Any

from monkeybot.core.logging_utils import kv
from monkeybot.core.types.types_tools import ToolDef

logger = logging.getLogger("monkeybot.core.runtime.loop.doom_loop")


def _effective_doom_loop_threshold() -> int:
    """Consecutive identical tool calls before doom-loop recovery.

    Default 3. Set ``DOOM_LOOP_THRESHOLD=0`` to disable. Applies whether the
    calls succeed or fail — identical name+args with no progress is the signal.
    """
    raw = os.getenv("DOOM_LOOP_THRESHOLD")
    if raw is None or raw.strip() == "":
        return 3
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "invalid DOOM_LOOP_THRESHOLD %s; using default 3",
            kv(value=raw),
        )
        return 3


def _tool_call_fingerprint(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True, default=str)}"


def _doom_loop_texts(tool_name: str, threshold: int) -> tuple[str, str]:
    """Return ``(error_event_text, recovery_system_note)``."""
    error = f"Doom loop detected: tool {tool_name!r} called identically {threshold} times"
    note = (
        f"[Harness] {error}. Do not repeat the same call. Explain the situation "
        "to the user and take a different approach (different tool or args)."
    )
    return error, note


_PATH_ARG_KEYS = ("path", "file_path", "file")
# One file section inside a Codex-style patch. ``*** Move to:`` is not a second target.
_PATCH_FILE_HEADER = re.compile(r"(?m)^\*\*\* (?:Add|Delete|Update) File: ([^\r\n]+?)\s*$")


def _canonical_tool_path(value: str) -> str:
    """Identity of the file a tool call targets.

    Relative spellings the workspace treats as one file (``./a``, ``a/./b``,
    ``a\\\\b``, ``a/../b``) share a streak. A leading ``/`` or ``~``, or a Windows
    drive, stays distinct so an absolute path does not collapse into a
    repo-relative one. ``..`` that would escape a relative path is kept, so
    repeated escapes still group together.
    """
    raw = value.strip()
    if not raw:
        return ""
    s = raw.replace("\\", "/")
    drive = ""
    if len(s) >= 2 and s[0].isalpha() and s[1] == ":":
        drive = s[:2]
        s = s[2:]
    absolute = bool(drive) or raw.startswith("/") or raw.startswith("~")
    leading_slash = s.startswith("/")
    stack: list[str] = []
    for part in s.split("/"):
        if not part or part == ".":
            continue
        if part == ".." and stack and stack[-1] not in ("..", "~"):
            stack.pop()
            continue
        stack.append(part)
    body = "/".join(stack)
    if drive:
        return f"{drive}/{body}" if body else drive
    if raw.startswith("~"):
        return body or "~"
    if absolute and leading_slash:
        return f"/{body}" if body else "/"
    return body


def _single_patch_path(patch_text: str) -> str | None:
    """The one file a patch edits, or ``None`` when it names zero or several."""
    found: list[str] = []
    for match in _PATCH_FILE_HEADER.finditer(patch_text):
        path = _canonical_tool_path(match.group(1))
        if path and path not in found:
            found.append(path)
        if len(found) > 1:
            return None
    return found[0] if found else None


def _failing_path_key(name: str, args: dict[str, Any]) -> tuple[str, str] | None:
    """``(tool, canonical path)`` when ``args`` target one file.

    Direct path arguments win. A patch with exactly one file section counts as
    that file, so a retry with a different hunk is still the same path.
    """
    saw_path_arg = False
    for key in _PATH_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            saw_path_arg = True
            path = _canonical_tool_path(value)
            if path:
                return (name, path)
    if saw_path_arg:
        return None
    patch_text = args.get("patch_text")
    if isinstance(patch_text, str):
        patch_path = _single_patch_path(patch_text)
        if patch_path:
            return (name, patch_path)
    return None


def _path_doom_loop_texts(tool_name: str, path: str, count: int) -> tuple[str, str]:
    """Return ``(error_event_text, recovery_system_note)`` for repeated failures on one path."""
    error = f"Doom loop detected: tool {tool_name!r} failed {count} times in a row on {path!r}"
    note = (
        f"[Harness] {error}. Guessing another variation of the same call is not working. "
        "Re-read the file to see its current content, then make one corrected change, or "
        "explain to the user what is blocking you."
    )
    return error, note


def _doom_loop_exempt_names(tools: Sequence[ToolDef]) -> frozenset[str]:
    """Tool names marked ``doom_loop_exempt`` (identical-args polling is expected)."""
    return frozenset(t.name for t in tools if t.doom_loop_exempt)


@dataclasses.dataclass
class _DoomLoopTracker:
    """Tracks consecutive identical tool calls within one user message.

    Fingerprint is tool name + args. Outcome (ok vs error) does not reset the
    streak — successful no-progress loops (e.g. repeated screenshots) must trip
    the same guard as repeated failures.

    A second detector catches the near-miss loop: the same tool failing on the same file
    ``threshold`` times in a row even though the other arguments differ (an edit retried with a
    slightly different ``old_string`` or hunk, or the same file spelled ``./a`` and ``a``).
    Only failures count there. A success, or any other non-exempt call, restarts the streak.
    Exempt tools are invisible to both detectors, so polling between failures does not
    hide the loop or trip it.

    Tools in ``exempt_names`` (from ``ToolDef.doom_loop_exempt``) are ignored so
    legitimate polling (e.g. ``loop_status``) does not force a recovery turn.

    After a recovery turn is consumed, the tracker re-arms so a later streak in
    the same user message can trigger again. While ``triggered`` is set (between
    detection and ``consume_recovery``), further ``record`` calls are ignored so
    the same streak cannot re-fire mid-batch.
    """

    threshold: int
    exempt_names: frozenset[str] = dataclasses.field(default_factory=frozenset)
    streak_fp: str | None = None
    streak_count: int = 0
    triggered: bool = False
    force_no_tools: bool = False
    recovery_note: str | None = None
    triggered_tool: str | None = None
    path_streak_key: tuple[str, str] | None = None
    path_streak_count: int = 0
    _pending_error: str | None = None

    def record(self, name: str, args: dict[str, Any], *, failed: bool = False) -> None:
        if self.threshold <= 0 or self.triggered or name in self.exempt_names:
            return
        fp = _tool_call_fingerprint(name, args)
        if fp == self.streak_fp:
            self.streak_count += 1
        else:
            self.streak_fp = fp
            self.streak_count = 1
        path_key = _failing_path_key(name, args) if failed else None
        if path_key is not None and path_key == self.path_streak_key:
            self.path_streak_count += 1
        else:
            self.path_streak_key = path_key
            self.path_streak_count = 1 if path_key is not None else 0
        if self.streak_count >= self.threshold:
            error, note = _doom_loop_texts(name, self.threshold)
        elif path_key is not None and self.path_streak_count >= self.threshold:
            error, note = _path_doom_loop_texts(name, path_key[1], self.path_streak_count)
        else:
            return
        self.triggered = True
        self.triggered_tool = name
        self._pending_error = error
        self.force_no_tools = True
        self.recovery_note = note

    def take_error(self) -> str | None:
        msg = self._pending_error
        self._pending_error = None
        return msg

    def consume_recovery(self) -> tuple[bool, str | None]:
        force = self.force_no_tools
        note = self.recovery_note
        self.force_no_tools = False
        self.recovery_note = None
        if force:
            # Re-arm so a second identical streak later in this user message
            # can trigger another recovery turn.
            self.triggered = False
            self.streak_fp = None
            self.streak_count = 0
            self.path_streak_key = None
            self.path_streak_count = 0
            self.triggered_tool = None
        return force, note
