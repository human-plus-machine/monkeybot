"""Doom-loop detection: consecutive identical tool calls within one user message."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
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
    error = (
        f"Doom loop detected: tool {tool_name!r} called identically "
        f"{threshold} times"
    )
    note = (
        f"[Harness] {error}. Do not repeat the same call. Explain the situation "
        "to the user and take a different approach (different tool or args)."
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
    _pending_error: str | None = None

    def record(self, name: str, args: dict[str, Any]) -> None:
        if self.threshold <= 0 or self.triggered or name in self.exempt_names:
            return
        fp = _tool_call_fingerprint(name, args)
        if fp == self.streak_fp:
            self.streak_count += 1
        else:
            self.streak_fp = fp
            self.streak_count = 1
        if self.streak_count < self.threshold:
            return
        self.triggered = True
        self.triggered_tool = name
        error, note = _doom_loop_texts(name, self.threshold)
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
            self.triggered_tool = None
        return force, note
