"""``CustomTool`` implementations that let the agent act on the user's Mac.

Every tool here does the hard-safety check (``computer/safety.py``) itself,
independent of whatever the soft ``permissions.yaml`` ask/allow ruleset decides
to prompt for — see the module docstring in ``safety.py`` for why.

Return convention matches the built-in tools' error envelope
(``core/tools/core_tool_executor.py:_built_in_tool_error``): a JSON string,
``{"ok": false, "error_kind": "validation"|"policy"|"runtime", "message", "hint"}``
on failure, ``{"ok": true, ...}`` on success.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from monkeybot.computer import safety
from monkeybot.core.tools.core_tool_executor import _built_in_tool_error
from monkeybot.core.types.types_tools import ToolDef

# Every tool name here must also appear in ``COMPUTER_TOOL_NAMES`` and
# ``ALWAYS_SCOPE`` in ``computer/__init__.py`` — the tests pin that invariant.


def _err(kind: safety.ErrorKind, message: str, hint: str) -> str:
    # Delegates to the built-in tools' own envelope builder instead of
    # duplicating its shape here, so the two can't drift apart.
    return _built_in_tool_error(kind, message, hint)


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False)


def _parse_limit(args: dict[str, object], *, default: int, cap: int) -> int:
    raw = args.get("limit")
    if isinstance(raw, (int, float, str)):
        try:
            return min(int(raw), cap)
        except (TypeError, ValueError):
            return default
    return default


def _run(fn: Callable[[], str]) -> str:
    """Run a tool body, translating ``ComputerToolError`` into the error envelope."""
    try:
        safety.require_macos()
        return fn()
    except safety.ComputerToolError as e:
        return _err(e.kind, e.message, e.hint)
    except OSError as e:
        return _err("runtime", str(e), "Check the path and permissions.")


class ComputerOpenTool:
    tool_def = ToolDef(
        "computer_open",
        "Open a file in its default app, or a folder in Finder, on the user's Mac. "
        "Set reveal=true to reveal (select) the item in Finder instead of opening it. "
        "Set app to open the file with a specific application instead of the default.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or ~-relative path to open."},
                "reveal": {
                    "type": "boolean",
                    "description": "Reveal in Finder instead of opening.",
                },
                "app": {
                    "type": "string",
                    "description": "Open with this application instead of the default.",
                },
            },
            "required": ["path"],
        },
    )

    async def execute(self, args: dict[str, object]) -> str:
        def body() -> str:
            raw_path = args.get("path")
            if not isinstance(raw_path, str):
                return _err("validation", "path is required", "Pass a path string.")
            reveal = bool(args.get("reveal", False))
            app = args.get("app")
            resolved = safety.resolve_user_path(raw_path, must_exist=True)
            if isinstance(app, str) and app.strip():
                safety.open_path_with_app(resolved, app)
                return _ok(path=str(resolved), opened_with=app)
            safety.open_path(resolved, reveal=reveal)
            return _ok(path=str(resolved), revealed=reveal)

        return _run(body)


class ComputerOpenURLTool:
    tool_def = ToolDef(
        "computer_open_url",
        "Open a URL in the user's default browser. http, https, and mailto only.",
        {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The URL to open."}},
            "required": ["url"],
        },
    )

    async def execute(self, args: dict[str, object]) -> str:
        def body() -> str:
            raw_url = args.get("url")
            if not isinstance(raw_url, str):
                return _err("validation", "url is required", "Pass a URL string.")
            url = safety.validate_url(raw_url)
            safety.open_url(url)
            return _ok(url=url)

        return _run(body)


class ComputerOpenAppTool:
    tool_def = ToolDef(
        "computer_open_app",
        "Launch an installed application by name (e.g. 'Notes', 'Calendar').",
        {
            "type": "object",
            "properties": {"app": {"type": "string", "description": "Application name."}},
            "required": ["app"],
        },
    )

    async def execute(self, args: dict[str, object]) -> str:
        def body() -> str:
            raw_app = args.get("app")
            if not isinstance(raw_app, str):
                return _err("validation", "app is required", "Pass an application name.")
            bundle = safety.resolve_app_bundle(raw_app)
            safety.open_app(bundle)
            return _ok(app=raw_app.strip())

        return _run(body)


class ComputerClipboardReadTool:
    tool_def = ToolDef(
        "computer_clipboard_read",
        "Read the current text on the user's clipboard.",
        {"type": "object", "properties": {}},
        parallel_safe=True,
    )

    async def execute(self, args: dict[str, object]) -> str:
        def body() -> str:
            text = safety.read_clipboard()
            return _ok(text=text, truncated=len(text) >= safety.MAX_CLIPBOARD_CHARS)

        return _run(body)


class ComputerClipboardWriteTool:
    tool_def = ToolDef(
        "computer_clipboard_write",
        "Write text to the user's clipboard.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to place on the clipboard."}
            },
            "required": ["text"],
        },
    )

    async def execute(self, args: dict[str, object]) -> str:
        def body() -> str:
            raw_text = args.get("text")
            if not isinstance(raw_text, str):
                return _err("validation", "text is required", "Pass a text string.")
            safety.write_clipboard(raw_text)
            return _ok(chars_written=len(raw_text))

        return _run(body)


class ComputerListDirTool:
    tool_def = ToolDef(
        "computer_list_dir",
        "List the contents of a directory on the user's Mac (name, type, size).",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to list."},
                "include_hidden": {
                    "type": "boolean",
                    "description": "Include dotfiles (default false).",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max entries (default 200, max {safety.MAX_LIST_ENTRIES}).",
                },
            },
            "required": ["path"],
        },
        parallel_safe=True,
    )

    async def execute(self, args: dict[str, object]) -> str:
        def body() -> str:
            raw_path = args.get("path")
            if not isinstance(raw_path, str):
                return _err("validation", "path is required", "Pass a directory path.")
            resolved = safety.resolve_user_path(raw_path, must_exist=True)
            if not resolved.is_dir():
                return _err("validation", f"Not a directory: {resolved}", "Pass a directory path.")
            include_hidden = bool(args.get("include_hidden", False))
            limit = _parse_limit(args, default=200, cap=safety.MAX_LIST_ENTRIES)

            entries: list[dict[str, Any]] = []
            truncated = False
            with os.scandir(resolved) as it:
                for entry in it:
                    if not include_hidden and entry.name.startswith("."):
                        continue
                    entry_path = Path(entry.path)
                    if safety.is_path_denied(entry_path):
                        continue
                    if len(entries) >= limit:
                        truncated = True
                        break
                    try:
                        stat = entry.stat(follow_symlinks=False)
                        entries.append(
                            {
                                "name": entry.name,
                                "is_dir": entry.is_dir(follow_symlinks=False),
                                "size_bytes": stat.st_size,
                                "modified_at": int(stat.st_mtime),
                            }
                        )
                    except OSError:
                        continue
            entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
            return _ok(path=str(resolved), entries=entries, truncated=truncated)

        return _run(body)


class ComputerFindTool:
    tool_def = ToolDef(
        "computer_find",
        "Search for files or folders by name under a directory on the user's Mac.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to search under."},
                "query": {
                    "type": "string",
                    "description": "Case-insensitive substring to match in the name.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["file", "dir", "any"],
                    "description": "Default 'any'.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max results (default 100, max {safety.MAX_FIND_RESULTS}).",
                },
            },
            "required": ["path", "query"],
        },
        parallel_safe=True,
    )

    async def execute(self, args: dict[str, object]) -> str:
        def body() -> str:
            raw_path = args.get("path")
            raw_query = args.get("query")
            if not isinstance(raw_path, str):
                return _err("validation", "path is required", "Pass a directory path.")
            if not isinstance(raw_query, str) or not raw_query.strip():
                return _err("validation", "query is required", "Pass a search string.")
            resolved_root = safety.resolve_user_path(raw_path, must_exist=True)
            if not resolved_root.is_dir():
                return _err(
                    "validation", f"Not a directory: {resolved_root}", "Pass a directory path."
                )
            kind = args.get("kind") or "any"
            limit = _parse_limit(args, default=100, cap=safety.MAX_FIND_RESULTS)
            query_lower = raw_query.strip().lower()

            results: list[dict[str, Any]] = []
            truncated = False
            for root, dirnames, filenames in os.walk(resolved_root):
                root_path = Path(root)
                dirnames[:] = [d for d in dirnames if not safety.is_path_denied(root_path / d)]
                if truncated:
                    break
                candidates: list[tuple[str, bool]] = []
                if kind in ("dir", "any"):
                    candidates += [(d, True) for d in dirnames]
                if kind in ("file", "any"):
                    candidates += [(f, False) for f in filenames]
                for name, is_dir in candidates:
                    if query_lower not in name.lower():
                        continue
                    candidate_path = root_path / name
                    if safety.is_path_denied(candidate_path):
                        continue
                    if len(results) >= limit:
                        truncated = True
                        break
                    results.append({"path": str(candidate_path), "is_dir": is_dir})

            return _ok(query=raw_query, results=results, truncated=truncated)

        return _run(body)


class ComputerMoveTool:
    tool_def = ToolDef(
        "computer_move",
        "Move or rename a file or folder on the user's Mac.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Item to move."},
                "destination": {
                    "type": "string",
                    "description": "New path (rename) or destination directory.",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Allow overwriting an existing destination.",
                },
            },
            "required": ["path", "destination"],
        },
    )

    async def execute(self, args: dict[str, object]) -> str:
        def body() -> str:
            raw_path = args.get("path")
            raw_dest = args.get("destination")
            if not isinstance(raw_path, str):
                return _err("validation", "path is required", "Pass the item to move.")
            if not isinstance(raw_dest, str):
                return _err("validation", "destination is required", "Pass a destination path.")
            src = safety.resolve_user_path(raw_path, must_exist=True)
            dest = safety.resolve_user_path(raw_dest, must_exist=False)
            overwrite = bool(args.get("overwrite", False))

            if dest.is_dir():
                dest = dest / src.name
                dest = safety.resolve_user_path(str(dest), must_exist=False)

            if src.is_dir() and (dest == src or safety.is_within(dest, src)):
                return _err(
                    "validation",
                    "Cannot move a folder into itself or a subfolder of itself.",
                    "Pick a different destination.",
                )
            if dest.exists() and not overwrite:
                return _err(
                    "validation",
                    f"Destination already exists: {dest}",
                    "Pass overwrite=true to replace it, or pick a different name.",
                )
            if dest.exists() and overwrite:
                # Trash the clobbered item rather than deleting it outright —
                # "trash, never delete" is a hard invariant of this tool family,
                # and an overwrite is exactly the kind of destructive step a
                # user could regret. check_trashable's protected-folder checks
                # also apply here as a second guard against clobbering something
                # like a top-level home folder.
                safety.trash_path(dest)

            shutil.move(str(src), str(dest))
            return _ok(path=str(src), destination=str(dest))

        return _run(body)


class ComputerTrashTool:
    tool_def = ToolDef(
        "computer_trash",
        "Move a file or folder to the Trash on the user's Mac. Never permanently deletes.",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Item to trash."}},
            "required": ["path"],
        },
    )

    async def execute(self, args: dict[str, object]) -> str:
        def body() -> str:
            raw_path = args.get("path")
            if not isinstance(raw_path, str):
                return _err("validation", "path is required", "Pass the item to trash.")
            resolved = safety.resolve_user_path(raw_path, must_exist=True)
            dest = safety.trash_path(resolved)
            return _ok(path=str(resolved), trashed_to=str(dest))

        return _run(body)
