"""Canonical tool-name -> display-kind mapping.

Shared by the CLI's chat display (``monkeybot_cli.chat_tool_display``) and
the core chat-history wire serializer (``core.persistence.thread_summary``)
so both surfaces label tools identically without hand-maintained duplicates.
"""

from __future__ import annotations

_TOOL_KIND: dict[str, str] = {
    "run_command": "Shell",
    "execute": "Shell",
    "shell": "Shell",
    "bash": "Shell",
    "read_file": "Read",
    "read": "Read",
    "write_file": "Write",
    "write": "Write",
    "edit_file": "Edit",
    "apply_patch": "Edit",
    "str_replace": "Edit",
    "grep": "Search",
    "search": "Search",
    "web_search": "Search",
    "glob": "Glob",
    "list_dir": "List",
    "list_directory": "List",
    "task": "Task",
}


def tool_kind_label(tool: str) -> str:
    """Short display kind for a tool name, e.g. ``run_command`` -> ``Shell``."""
    key = tool.strip().lower().replace("-", "_")
    kind = _TOOL_KIND.get(key)
    if kind is not None:
        return kind
    cleaned = tool.strip().replace("_", " ").replace("-", " ")
    return cleaned.title() if cleaned else "Tool"
