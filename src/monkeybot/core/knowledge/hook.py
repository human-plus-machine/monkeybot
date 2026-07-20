"""POST_TOOL hooks that keep the knowledge index fresh after file writes."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.knowledge.indexer import KnowledgeIndexer

logger = logging.getLogger(__name__)

_WRITE_TOOLS = frozenset(
    {
        "write_file",
        "replace_in_file",
        "apply_patch",
    }
)

# Shell / FS mutations that bypass file tools (e.g. git clone) need a workspace rescan.
_SHELL_TOOLS = frozenset({"run_command"})

_READ_ONLY_CMDS = frozenset(
    {
        "ls",
        "ll",
        "dir",
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "fd",
        "pwd",
        "echo",
        "printf",
        "which",
        "type",
        "whoami",
        "id",
        "printenv",
        "wc",
        "stat",
        "file",
        "tree",
        "du",
        "df",
        "date",
        "true",
        "false",
        "test",
        "[",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "uname",
        "hostname",
        "sleep",
    }
)

_MUTATING_CMDS = frozenset(
    {
        "git",
        "mv",
        "cp",
        "rm",
        "rmdir",
        "mkdir",
        "touch",
        "chmod",
        "chown",
        "ln",
        "tar",
        "unzip",
        "gzip",
        "gunzip",
        "zip",
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "pip",
        "pip3",
        "uv",
        "cargo",
        "make",
        "cmake",
        "docker",
        "podman",
        "sed",
        "tee",
        "install",
        "rsync",
        "scp",
    }
)

# Wrappers peeled so the real command is classified.
_SHELL_WRAPPERS = frozenset(
    {
        "sudo",
        "env",
        "nice",
        "nohup",
        "time",
        "command",
        "xargs",
        "stdbuf",
    }
)

_GIT_READONLY_SUBS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "branch",
        "remote",
        "rev-parse",
        "describe",
        "ls-files",
        "ls-tree",
        "cat-file",
        "blame",
        "shortlog",
    }
)

_GIT_STASH_READONLY_SUBS = frozenset({"list", "show"})

_PKG_MUTATING_SUBS = frozenset(
    {
        "install",
        "i",
        "add",
        "ci",
        "build",
        "uninstall",
        "remove",
        "rm",
        "sync",
        "update",
        "upgrade",
        "run",
    }
)

_FIND_MUTATING_FLAGS = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir"}
)

# Include fd redirects (``2>/tmp/out``); require a command-boundary so bare
# comparison operators inside ``[[ … ]]`` are less likely to false-positive.
_REDIRECT_RE = re.compile(r"(?:^|[\s;|&])\d*>{1,2}|<<|\|\s*tee\b")
_COMPOUND_SPLIT_RE = re.compile(r"&&|\|\||;")


class KnowledgeHook:
    """Enqueue dirty workspace/note paths after successful write / shell tools."""

    def __init__(self, indexer: KnowledgeIndexer) -> None:
        self._indexer = indexer
        self._notes_dirty = False

    def register(self, manager: HookManager) -> None:
        manager.register(HookEvent.POST_TOOL, self.on_post_tool)
        manager.register(HookEvent.POST_TURN, self.on_post_turn)

    async def on_post_tool(self, payload: HookPayload) -> None:
        if not payload.tool_name:
            return
        if payload.tool_error:
            return

        if payload.tool_name in _WRITE_TOOLS:
            for rel in _paths_from_tool(
                payload.tool_name, payload.tool_args, payload.tool_result
            ):
                self._indexer.enqueue_workspace_rel(rel)
                if _looks_like_note_path(rel):
                    self._notes_dirty = True
            return

        if payload.tool_name in _SHELL_TOOLS and _run_command_succeeded(payload.tool_result):
            argv = _argv_from_args(payload.tool_args)
            if not command_implies_fs_mutation(argv):
                logger.debug(
                    "knowledge: skip rescan for read-only run_command argv=%r", argv
                )
                return
            logger.debug("knowledge: scheduling workspace rescan after run_command")
            self._indexer.request_workspace_rescan()

    async def on_post_turn(self, payload: HookPayload) -> None:
        del payload
        if not self._notes_dirty:
            return
        self._notes_dirty = False
        self._indexer.request_notes_rescan()


def _looks_like_note_path(rel: str) -> bool:
    norm = rel.replace("\\", "/").lstrip("./")
    return (
        norm.startswith("notes/")
        or "/notes/" in norm
    )


def command_implies_fs_mutation(argv: list[str] | None) -> bool:
    """Return True when argv may change workspace files.

    Unknown commands default to rescan (fail-safe) so wrappers like
    ``python3 -c`` / ``node -e`` cannot silently leave the index stale.
    """
    if not argv:
        return False
    joined = " ".join(argv)
    if _REDIRECT_RE.search(joined):
        return True
    return _argv_implies_mutation(list(argv))


def _argv_implies_mutation(argv: list[str]) -> bool:
    argv = _peel_wrappers(argv)
    if not argv:
        return False

    cmd = argv[0].rsplit("/", 1)[-1].lower()

    # bash -c / sh -c — inspect every compound segment of the script body
    if cmd in {"bash", "sh", "zsh", "fish"} and len(argv) >= 3 and argv[1] in {
        "-c",
        "-lc",
        "-ic",
    }:
        return _script_implies_mutation(argv[2])

    if cmd == "find":
        return _find_implies_mutation(argv[1:])

    if cmd in _READ_ONLY_CMDS:
        return False

    if cmd == "git":
        return _git_implies_mutation(argv)

    if cmd in _MUTATING_CMDS:
        if cmd in {"npm", "pnpm", "yarn", "bun", "pip", "pip3", "uv"} and len(argv) >= 2:
            sub = argv[1].lstrip("-").lower()
            if sub in {
                "list",
                "ls",
                "outdated",
                "view",
                "info",
                "test",
                "exec",
                "why",
                "tree",
            }:
                return False
            return sub in _PKG_MUTATING_SUBS
        return True

    # Unknown binary — assume it may mutate (safe default for index freshness).
    return True


def _peel_wrappers(argv: list[str]) -> list[str]:
    out = list(argv)
    while out:
        head = out[0].rsplit("/", 1)[-1].lower()
        if head not in _SHELL_WRAPPERS:
            break
        out = out[1:]
        if head == "env":
            while out and "=" in out[0] and not out[0].startswith("-"):
                out = out[1:]
        elif head == "sudo":
            while out and out[0].startswith("-") and out[0] not in {"-", "--"}:
                # sudo -u user / sudo -E … — drop flags; keep operand.
                if out[0] in {"-u", "-g", "-h", "-p", "-r", "-t", "-C"} and len(out) >= 2:
                    out = out[2:]
                else:
                    out = out[1:]
    return out


def _script_implies_mutation(script: str) -> bool:
    if _REDIRECT_RE.search(script):
        return True
    # Split on pipes after compound ops so ``a | b`` still classifies both sides.
    for compound in _COMPOUND_SPLIT_RE.split(script):
        for segment in compound.split("|"):
            tokens = segment.strip().split()
            if not tokens:
                continue
            if _argv_implies_mutation(tokens):
                return True
    return False


def _find_implies_mutation(args: list[str]) -> bool:
    for arg in args:
        if arg in _FIND_MUTATING_FLAGS or arg.startswith("-exec"):
            return True
    return False


def _git_implies_mutation(argv: list[str]) -> bool:
    if len(argv) < 2:
        return False
    sub = argv[1].lstrip("-")
    if sub == "stash":
        if len(argv) >= 3 and argv[2].lstrip("-") in _GIT_STASH_READONLY_SUBS:
            return False
        return True
    if sub in _GIT_READONLY_SUBS:
        return False
    return True


def _argv_from_args(args: dict[str, Any] | None) -> list[str] | None:
    if not args:
        return None
    argv = args.get("argv")
    if isinstance(argv, list) and all(isinstance(x, str) for x in argv):
        return list(argv)
    # Legacy shapes
    cmd = args.get("command")
    extra = args.get("args") or args.get("arguments")
    if isinstance(cmd, str) and cmd.strip():
        parts = [cmd.strip()]
        if isinstance(extra, list):
            parts.extend(str(x) for x in extra)
        return parts
    return None


def _run_command_succeeded(result: str | None) -> bool:
    """True when run_command returned ok / exit_code 0 (best-effort parse)."""
    if not result:
        return True  # no body but no tool_error → treat as success
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return True
    if not isinstance(data, dict):
        return True
    if data.get("ok") is False:
        return False
    exit_code = data.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return False
    return True


def _paths_from_tool(
    tool_name: str,
    args: dict[str, Any] | None,
    result: str | None,
) -> list[str]:
    args = args or {}
    paths: list[str] = []

    if tool_name in {"write_file", "replace_in_file"}:
        path = args.get("path") or args.get("file") or args.get("filename")
        if isinstance(path, str) and path.strip():
            paths.append(path.strip())
        return paths

    if tool_name == "apply_patch":
        # Prefer paths reported in the tool result JSON
        if result:
            try:
                data = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                data = None
            if isinstance(data, dict):
                for key in ("written", "updated", "deleted", "paths", "files"):
                    val = data.get(key)
                    if isinstance(val, list):
                        for item in val:
                            if isinstance(item, str):
                                paths.append(item)
                            elif isinstance(item, dict):
                                p = item.get("path") or item.get("file")
                                if isinstance(p, str):
                                    paths.append(p)
        # Also scrape patch_text headers when result lacked paths
        patch = args.get("patch_text") or args.get("patch")
        if isinstance(patch, str):
            for line in patch.splitlines():
                for prefix in ("*** Add File: ", "*** Update File: ", "*** Delete File: "):
                    if line.startswith(prefix):
                        paths.append(line[len(prefix) :].strip())
        return paths

    return paths


__all__ = ["KnowledgeHook", "command_implies_fs_mutation"]
