"""Tool activity display helpers for ``monkeybot chat``."""

from __future__ import annotations

from pathlib import Path

from monkeybot.core.tools.tool_hint import (
    DETAIL_MAX,
    TITLE_HINT_MAX,
    collapse_hint,
    resolve_tool_hint,
    task_hint,
    task_subagent_label,
    tool_collapsed_title,
    tool_hint,
    truncate_detail,
    truncate_subagent_hint,
)

# Re-export shared helpers so existing CLI imports keep working.
__all__ = [
    "DETAIL_MAX",
    "TITLE_HINT_MAX",
    "collapse_hint",
    "format_tool_expand_body",
    "resolve_tool_hint",
    "task_hint",
    "task_subagent_label",
    "tool_collapsed_title",
    "tool_display",
    "tool_hint",
    "tool_spinner_prefix",
    "truncate_subagent_hint",
]

_SHELL_TAIL_LINES = 40

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
    max_chars: int = DETAIL_MAX,
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
        parts.append(_fence(truncate_detail(result, max_chars), lang))
    elif key in _EDIT_TOOLS and _looks_like_diff(result):
        parts.append(_fence(truncate_detail(result, max_chars), "diff"))
    elif key in _SHELL_TOOLS:
        parts.append(_fence(truncate_detail(_tail_lines(result), max_chars), ""))
    elif key in _SEARCH_TOOLS:
        parts.append(_format_search_result(result, max_chars))
    else:
        text = truncate_detail(result, max_chars)
        parts.append(_fence(text, ""))

    return "\n".join(parts)
