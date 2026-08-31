"""Local computer-control tools (macOS only, desktop-only, opt-in).

Lets the agent open files/folders/apps/URLs, read and write the clipboard, and
browse/move/trash files anywhere under the user's home directory — the actions
needed for requests like "open my Downloads folder" or "what's on my clipboard".

Off by default and gated behind ``MONKEYBOT_COMPUTER_TOOLS`` (mapped from
``computer.enabled`` in ``monkeybot.yaml``) so server/Cloud Run deployments never
advertise these tools; the desktop app (monkeyapp) is the only intended caller,
and only ever for a locally spawned gateway process. See ``safety.py`` for why
the hard security boundary lives in the tool bodies rather than in the (soft,
user-editable, fail-open) permission ruleset, and ``permissions.py`` for how
the ask-by-default baseline and durable "Always allow" approvals are layered
into that ruleset.
"""

from __future__ import annotations

import sys

from monkeybot.computer.approvals import Scope as AlwaysScope
from monkeybot.computer.tools import (
    ComputerClipboardReadTool,
    ComputerClipboardWriteTool,
    ComputerFindTool,
    ComputerListDirTool,
    ComputerMoveTool,
    ComputerOpenAppTool,
    ComputerOpenTool,
    ComputerOpenURLTool,
    ComputerTrashTool,
)
from monkeybot.core.config.snapshot import RuntimeConfig, current_env_flag, env_flag
from monkeybot.core.context import CustomTool

__all__ = [
    "ALWAYS_SCOPE",
    "COMPUTER_TOOL_NAMES",
    "AlwaysScope",
    "build_computer_tools",
    "computer_tools_enabled_from_env",
    "is_computer_tool_name",
    "should_enable_computer_tools",
]

COMPUTER_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "computer_open",
        "computer_open_url",
        "computer_open_app",
        "computer_clipboard_read",
        "computer_clipboard_write",
        "computer_list_dir",
        "computer_find",
        "computer_move",
        "computer_trash",
    }
)

# Tools eligible for a durable "Always allow" rule, and at what granularity.
# `computer_move` / `computer_trash` are deliberately absent: their permission
# resource is derived from the *source* path only (see
# `permission.resource_for_call`), so an "always" rule keyed on the source
# would let the model move or trash that item to/as *anything* on every future
# call — the destination is never part of what was approved. Excluding them
# means the UI never offers "Always allow" for a mutating tool; every call
# asks again by design. `computer_open_app` is also excluded: launching an
# app is broad and low-friction to re-approve, and a standing grant from a
# single approval is more than that one click should buy.
ALWAYS_SCOPE: dict[str, AlwaysScope] = {
    "computer_open": "resource",
    "computer_open_url": "resource",
    "computer_clipboard_read": "tool",
    "computer_clipboard_write": "tool",
    "computer_list_dir": "resource",
    "computer_find": "resource",
}


def is_computer_tool_name(name: str) -> bool:
    return name in COMPUTER_TOOL_NAMES


def computer_tools_enabled_from_env(cfg: RuntimeConfig | None = None) -> bool:
    """True only when explicitly opted in (default OFF — unlike ``todo_list``).

    Reads ``MONKEYBOT_COMPUTER_TOOLS`` (mapped from ``computer.enabled`` in
    monkeybot.yaml). Recognized on values: ``1``, ``true``, ``yes``, ``on``.
    When ``cfg`` is given, the pinned snapshot is used instead of process env.
    """
    if cfg is not None:
        return env_flag(cfg, "MONKEYBOT_COMPUTER_TOOLS", default=False)
    return current_env_flag("MONKEYBOT_COMPUTER_TOOLS", default=False)


def should_enable_computer_tools(cfg: RuntimeConfig | None = None) -> bool:
    """Combines the env opt-in with the hard macOS-only platform gate."""
    return computer_tools_enabled_from_env(cfg) and sys.platform == "darwin"


def build_computer_tools() -> list[CustomTool]:
    """Instantiate all ``computer_*`` CustomTool objects.

    Callers should only invoke this behind :func:`should_enable_computer_tools`;
    on a non-macOS gateway the tools would simply fail every call via
    ``safety.require_macos()`` and shouldn't be advertised to the model at all.
    """
    return [
        ComputerOpenTool(),
        ComputerOpenURLTool(),
        ComputerOpenAppTool(),
        ComputerClipboardReadTool(),
        ComputerClipboardWriteTool(),
        ComputerListDirTool(),
        ComputerFindTool(),
        ComputerMoveTool(),
        ComputerTrashTool(),
    ]
