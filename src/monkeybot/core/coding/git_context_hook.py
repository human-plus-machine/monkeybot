"""``PRE_TURN`` hook: tell the model which branch it is on and what is already dirty.

The text lands in ``## Runtime notes`` (volatile), never in the cached prefix, so git
state can change every turn without touching the prompt cache.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload

logger = logging.getLogger(__name__)

# Well under the hook manager's 2s read-hook bound, so a slow repo costs one second, not a stall.
GIT_STATUS_TIMEOUT_S = 1.0
_MAX_LISTED_PATHS = 5
# These override ``git -C <workspace>`` and would report some other repo (pre-commit and
# direnv export ``GIT_DIR``). Status is always about the workspace we were given.
_GIT_REPO_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    }
)


def _display_path(raw: str) -> str:
    path = raw.split(" -> ")[-1].strip()
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        path = path[1:-1]
    return path


def format_git_status(porcelain: str) -> str | None:
    """Render ``git status --porcelain -b`` output as one line, or ``None`` if unparseable."""
    lines = [ln for ln in porcelain.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("## "):
        return None
    header = lines[0][3:]
    if header.startswith("No commits yet on "):
        branch = f"{header.removeprefix('No commits yet on ')} (no commits yet)"
    elif header.startswith("HEAD (no branch)"):
        branch = "(detached HEAD)"
    else:
        branch = header.split("...")[0]
        if "[" in header:
            branch += " " + header[header.index("[") :]
    changed: list[str] = []
    untracked = 0
    for line in lines[1:]:
        if line.startswith("??"):
            untracked += 1
        else:
            changed.append(_display_path(line[3:]))
    parts = [f"branch {branch}"]
    if not changed and not untracked:
        parts.append("clean")
    if changed:
        listed = ", ".join(changed[:_MAX_LISTED_PATHS])
        if len(changed) > _MAX_LISTED_PATHS:
            listed += f", +{len(changed) - _MAX_LISTED_PATHS} more"
        parts.append(f"{len(changed)} modified ({listed})")
    if untracked:
        parts.append(f"{untracked} untracked")
    return "git: " + ", ".join(parts)


def git_workspace_env() -> dict[str, str]:
    """Environment for a git command about a chosen directory, not the ambient repo."""
    env = {key: value for key, value in os.environ.items() if key not in _GIT_REPO_ENV}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    return env


async def git_status_note(
    workspace_root: Path, *, timeout_s: float = GIT_STATUS_TIMEOUT_S
) -> str | None:
    """One-line git state of ``workspace_root``; ``None`` when not a repo, git is missing, or slow."""
    env = git_workspace_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(workspace_root),
            "status",
            "--porcelain",
            "-b",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except OSError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        # A grandchild can keep the pipe open past the kill; never let reaping stall the turn.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=0.5)
        logger.debug("git status timed out in %s", workspace_root)
        return None
    if proc.returncode != 0:
        return None
    return format_git_status(stdout.decode("utf-8", errors="replace"))


class GitContextHook:
    """Registers the ``PRE_TURN`` git-state note (coding mode, main agent only)."""

    def register(self, manager: HookManager) -> None:
        manager.register(HookEvent.PRE_TURN, self.on_pre_turn)

    async def on_pre_turn(self, payload: HookPayload) -> None:
        root = payload.ctx.workspace_root
        if root is None:
            return
        note = await git_status_note(Path(root))
        if note:
            payload.inject_text = f"{payload.inject_text}\n{note}" if payload.inject_text else note
