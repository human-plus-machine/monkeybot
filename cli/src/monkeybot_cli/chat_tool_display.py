"""Tool activity display helpers for ``monkeybot chat``."""

from __future__ import annotations

from pathlib import Path

_SUBAGENT_HINT_MAX = 60
_TITLE_HINT_MAX = 72
_DETAIL_MAX = 8000
_SHELL_TAIL_LINES = 40

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

_SHELL_TOOLS = frozenset({"run_command", "execute", "shell", "bash"})
_READ_TOOLS = frozenset({"read_file", "read"})
_EDIT_TOOLS = frozenset(
    {"write_file", "write", "edit_file", "apply_patch", "str_replace"}
)
_PATH_TOOLS = _READ_TOOLS | _EDIT_TOOLS
_SEARCH_TOOLS = frozenset({"grep", "search", "web_search"})
_HINT_KEYS = frozenset(
    {
        "argv",
        "command",
        "args",
        "arguments",
        "shell",
        "script",
        "path",
        "query",
        "url",
        "task",
        "instructions",
        "prompt",
        "objective",
        "subagent_type",
        "type",
        "persona",
    }
)

_LANG_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".json": "json",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".rs": "rust",
    ".go": "go",
    ".sh": "bash",
    ".css": "css",
    ".html": "html",
    ".sql": "sql",
}


def collapse_hint(text: str) -> str:
    return " ".join(text.split())


def truncate_subagent_hint(text: str) -> str:
    collapsed = collapse_hint(text)
    if len(collapsed) <= _SUBAGENT_HINT_MAX:
        return collapsed
    return collapsed[:_SUBAGENT_HINT_MAX] + "…"


def tool_hint(args: dict[str, object]) -> str:
    """Summary of tool args for status lines."""
    argv = args.get("argv")
    if isinstance(argv, list) and argv:
        return collapse_hint(" ".join(str(x) for x in argv))

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


def tool_kind_label(tool: str) -> str:
    key = tool.strip().lower().replace("-", "_")
    if key in _TOOL_KIND:
        return _TOOL_KIND[key]
    cleaned = tool.strip().replace("_", " ").replace("-", " ")
    return cleaned.title() if cleaned else "Tool"


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
    if hint and len(hint) > _TITLE_HINT_MAX:
        hint = hint[:_TITLE_HINT_MAX] + "…"
    return f"{kind}  {hint}" if hint else kind


def tool_display(tool: str, label: str, args: dict[str, object]) -> str:
    """Plain-path activity line: ``run_command — echo hi``."""
    hint = resolve_tool_hint(tool, label, args)
    if tool == "task":
        base = task_subagent_label(args)
        return base + (f" — {hint}" if hint else "")
    return tool + (f" — {hint}" if hint else "")


def tool_spinner_prefix(tool: str, label: str, args: dict[str, object]) -> str:
    if tool == "task":
        hint = resolve_tool_hint(tool, label, args)
        base = "spawning " + task_subagent_label(args)
        return base + (f" — {hint}" if hint else "")
    return tool_display(tool, label, args)


def _tool_key(tool: str) -> str:
    return tool.strip().lower().replace("-", "_")


def _primary_section_label(tool: str) -> str:
    key = _tool_key(tool)
    if key in _SHELL_TOOLS:
        return "Command"
    if key in _PATH_TOOLS:
        return "Path"
    if key in _SEARCH_TOOLS:
        return "Query"
    if key == "task":
        return "Task"
    return "Input"


def _path_from_args(args: dict[str, object]) -> str:
    path = args.get("path")
    return path.strip() if isinstance(path, str) else ""


def _language_for_path(path: str) -> str:
    if not path:
        return ""
    return _LANG_BY_SUFFIX.get(Path(path).suffix.lower(), "")


def _looks_like_diff(text: str) -> bool:
    has_hunk = False
    has_file = False
    for line in text.splitlines()[:40]:
        if line.startswith("@@"):
            has_hunk = True
        if line.startswith("--- ") or line.startswith("+++ "):
            has_file = True
        if has_hunk and has_file:
            return True
    return False


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n…"


def _tail_lines(text: str, n: int = _SHELL_TAIL_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    omitted = len(lines) - n
    return f"… ({omitted} earlier lines)\n" + "\n".join(lines[-n:])


def _fence(body: str, lang: str = "") -> str:
    fence = "```"
    while fence in body:
        fence += "`"
    return f"{fence}{lang}\n{body.rstrip()}\n{fence}"


def _format_search_result(result: str, max_chars: int) -> str:
    lines: list[str] = []
    for raw in result.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("http://") or line.startswith("https://"):
            lines.append(f"- <{line}>")
        elif "://" in line and " " in line:
            # "Title https://..." style
            parts = line.rsplit(None, 1)
            if len(parts) == 2 and parts[1].startswith("http"):
                lines.append(f"- [{parts[0]}]({parts[1]})")
            else:
                lines.append(f"- `{line}`")
        else:
            lines.append(f"- `{line}`")
        if sum(len(x) for x in lines) > max_chars:
            lines.append("- …")
            break
    return "\n".join(lines) if lines else "_empty_"


def _extras_markdown(args: dict[str, object]) -> list[str]:
    extras = [
        f"- `{key}`: `{val}`"
        if isinstance(val, (str, int, bool, float)) or val is None
        else f"- `{key}`: `{val!r}`"[:200]
        for key, val in args.items()
        if key not in _HINT_KEYS
    ]
    if not extras:
        return []
    return ["", "**Args**", *extras]


def format_tool_expand_body(
    tool: str,
    args: dict[str, object],
    *,
    result: str = "",
    error: object = None,
    max_chars: int = _DETAIL_MAX,
) -> str:
    """Human-readable expand body as markdown (not raw JSON dump)."""
    key = _tool_key(tool)
    parts: list[str] = []
    primary = resolve_tool_hint(tool, tool, args)
    if primary:
        parts.append(f"**{_primary_section_label(tool)}**")
        parts.append(f"`{primary}`")

    parts.extend(_extras_markdown(args))

    if error:
        if parts:
            parts.append("")
        parts.append("**Error**")
        parts.append(f"```\n{error}\n```")
        return "\n".join(parts)

    if not result:
        return "\n".join(parts) if parts else "(no details)"

    if parts:
        parts.append("")
    parts.append("**Result**")

    if key in _READ_TOOLS:
        lang = _language_for_path(_path_from_args(args))
        parts.append(_fence(_truncate(result, max_chars), lang))
    elif key in _EDIT_TOOLS and _looks_like_diff(result):
        parts.append(_fence(_truncate(result, max_chars), "diff"))
    elif key in _SHELL_TOOLS:
        parts.append(_fence(_truncate(_tail_lines(result), max_chars), ""))
    elif key in _SEARCH_TOOLS:
        parts.append(_format_search_result(result, max_chars))
    else:
        text = _truncate(result, max_chars)
        parts.append(_fence(text, ""))

    return "\n".join(parts)
