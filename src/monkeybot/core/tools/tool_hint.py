"""Shared tool-arg hints and collapsed titles for CLI display and chat-history wire.

Keeps the CLI and ``messages_to_wire`` on one hint/title implementation so
titles like ``Shell  ls`` do not drift between surfaces.
"""

from __future__ import annotations

from monkeybot.core.tools.inspector import coerce_run_command_argv
from monkeybot.core.tools.tool_kind import tool_kind_label

SUBAGENT_HINT_MAX = 60
TITLE_HINT_MAX = 72
DETAIL_MAX = 8000


def collapse_hint(text: str) -> str:
    return " ".join(text.split())


def truncate_subagent_hint(text: str) -> str:
    collapsed = collapse_hint(text)
    if len(collapsed) <= SUBAGENT_HINT_MAX:
        return collapsed
    return collapsed[:SUBAGENT_HINT_MAX] + "…"


def tool_hint(args: dict[str, object]) -> str:
    """Summary of tool args for status lines and wire titles."""
    try:
        argv = coerce_run_command_argv(args.get("argv"))
    except ValueError:
        argv = None
    if argv:
        return collapse_hint(" ".join(argv))

    cmd = args.get("command")
    if isinstance(cmd, str) and cmd.strip():
        extra = args.get("args")
        if extra is None:
            extra = args.get("arguments")
        if isinstance(extra, list) and extra:
            line = " ".join([cmd.strip(), *[str(x) for x in extra]])
        else:
            line = cmd.strip()
        return collapse_hint(line)

    for key in ("shell", "script", "path", "query", "url"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return collapse_hint(val.strip())
    keys = list(args.keys())
    if len(keys) == 1:
        key = keys[0]
        val = args[key]
        if isinstance(val, (str, int, bool, float)):
            return collapse_hint(f"{key}: {val}")
    if keys:
        return f"{len(keys)} arg{'s' if len(keys) != 1 else ''}"
    return ""


def task_subagent_label(args: dict[str, object]) -> str:
    for key in ("subagent_type", "type", "persona"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return f"subagent:{val.strip()}"
    return "subagent"


def task_hint(args: dict[str, object]) -> str:
    for key in ("task", "instructions", "prompt", "objective"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return truncate_subagent_hint(val.strip())
    return ""


def resolve_tool_hint(tool: str, label: str, args: dict[str, object]) -> str:
    """Shared hint used by collapsed titles and plain-path display."""
    if tool == "task":
        hint = task_hint(args)
        if not hint and label.strip() and label.strip() != tool:
            return truncate_subagent_hint(label.strip())
        return hint
    hint = tool_hint(args)
    if not hint and label.strip() and label.strip() != tool:
        return collapse_hint(label.strip())
    return hint


def tool_collapsed_title(tool: str, label: str, args: dict[str, object]) -> str:
    """Short Cursor/Claude-style title: ``Shell  git status``."""
    hint = resolve_tool_hint(tool, label, args)
    if tool == "task":
        base = task_subagent_label(args)
        kind = "Task" if base == "subagent" else base
    else:
        kind = tool_kind_label(tool)
    if hint and len(hint) > TITLE_HINT_MAX:
        hint = hint[:TITLE_HINT_MAX] + "…"
    return f"{kind}  {hint}" if hint else kind


def truncate_detail(text: str, max_chars: int = DETAIL_MAX) -> str:
    """Cap long tool args/results for chat-history wire and expand bodies."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def truncate_wire_args(
    args: dict[str, object], *, max_chars: int = DETAIL_MAX
) -> dict[str, object]:
    """Return a shallow copy with oversize string values truncated."""
    out: dict[str, object] = {}
    for key, val in args.items():
        if isinstance(val, str) and len(val) > max_chars:
            out[key] = truncate_detail(val, max_chars)
        else:
            out[key] = val
    return out
