"""Turn-end verification gate (coding mode).

A final answer that follows file edits nobody tested is the most common way a coding agent
"finishes" wrongly. This hook refuses such an answer once (``coding.verification_gate``),
tells the model what changed since its last passing test/lint run, and lets it either run
the checks or say plainly that it could not. All policy lives here; the loop only offers the
generic ``BEFORE_PROVIDER_REQUEST.hold_assistant_text`` / ``PRE_FINAL_ANSWER`` seam.

Subagents run without a hook manager and are therefore not gated.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from monkeybot.core.config.settings import VerificationGateConfig
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.tools.inspector import (
    UnscreenableScript,
    _split_shell_script,
    _strip_command_prefixes,
    parse_run_command,
    shell_script_arg,
    shell_script_segments,
)
from monkeybot.core.tools.read_ledger import CommandRecord
from monkeybot.core.verifier.match import WRITE_TOOLS, path_args

logger = logging.getLogger(__name__)

_THREAD_STATE_CAP = 256
_MAX_LISTED = 5
# Commands that read, list, move, or only set up the shell never run a project's checks,
# even when an argument is called "pytest" or "ruff.toml".
_NON_EXECUTING = frozenset(
    {
        "cat",
        "echo",
        "printf",
        "ls",
        "grep",
        "rg",
        "head",
        "tail",
        "wc",
        "diff",
        "sed",
        "awk",
        "find",
        "less",
        "more",
        "which",
        "file",
        "stat",
        "touch",
        "mkdir",
        "cp",
        "mv",
        "rm",
        "git",
        "cd",
        "pushd",
        "popd",
        "pwd",
        "true",
        "false",
        "export",
        "unset",
        "alias",
        "wait",
        ":",
    }
)
_MAX_SHELL_WALK = 8


@dataclass
class _MessageState:
    """What happened since the user's latest message."""

    unverified: dict[str, None] = field(default_factory=dict)  # ordered set of changed paths
    wrote: bool = False
    todo_touched: bool = False
    rejections: int = 0


def _tool_result_json(payload: HookPayload) -> dict[str, object] | None:
    raw = payload.tool_result
    if not raw or not raw.lstrip().startswith("{"):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _changed_paths(payload: HookPayload) -> list[str]:
    """Workspace paths a successful write tool call changed."""
    args = payload.tool_args or {}
    paths = list(path_args(args))
    if payload.tool_name == "apply_patch":
        result = _tool_result_json(payload)
        files = result.get("files") if result else None
        paths = [
            str(entry["path"])
            for entry in (files if isinstance(files, list) else [])
            if isinstance(entry, dict) and entry.get("path")
        ]
    return paths


def _preview(items: list[str]) -> str:
    shown = ", ".join(items[:_MAX_LISTED])
    if len(items) > _MAX_LISTED:
        shown += f", +{len(items) - _MAX_LISTED} more"
    return shown


def _unquoted_exit_is_masked(script: str) -> bool:
    """True when a zero exit of ``script`` does not prove each part of it succeeded.

    ``&&`` chains are safe: every part ran and succeeded. ``||``, ``;``, pipes, background
    ``&``, and command substitutions can exit 0 while the check failed or never ran.
    """
    quote = ""
    i = 0
    n = len(script)
    while i < n:
        ch = script[i]
        if quote == "'":
            if ch == "'":
                quote = ""
            i += 1
            continue
        if quote == '"':
            # Command substitution still runs inside double quotes.
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if script.startswith("$(", i) or ch == "`":
                return True
            if ch == '"':
                quote = ""
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if script.startswith("&&", i):
            i += 2
            continue
        if ch == "&":
            prev = script[i - 1] if i else ""
            nxt = script[i + 1] if i + 1 < n else ""
            # ``2>&1`` and ``&>file`` are redirects. A bare ``&`` backgrounds the command.
            if prev in {"<", ">"} or nxt in {"<", ">"}:
                i += 1
                continue
            return True
        if script.startswith("||", i) or script.startswith("$(", i) or ch in ";|\n`":
            return True
        i += 1
    return bool(quote)


def _argv_masks_exit(argv: list[str], depth: int = 0) -> bool:
    """True when ``argv`` (after wrappers) hides a check behind shell control flow."""
    if depth > _MAX_SHELL_WALK:
        return True
    script = shell_script_arg(_strip_command_prefixes(argv))
    if script is None:
        return False
    if _unquoted_exit_is_masked(script):
        return True
    try:
        lines = _split_shell_script(script, depth)
    except (ValueError, UnscreenableScript):
        return True
    for line in lines:
        try:
            nested = shlex.split(line)
        except ValueError:
            return True
        if _argv_masks_exit(nested, depth + 1):
            return True
    return False


def _pattern_matches_argv(patterns: Sequence[re.Pattern[str]], argv: list[str]) -> bool:
    """True when a pattern covers whole argv tokens, not a substring of one token.

    ``python -m pytest`` matches. ``python -c "print('pytest')"`` does not: ``pytest`` sits
    inside the code token, so a successful python process is not a test run.
    """
    if not argv:
        return False
    line = " ".join(argv)
    spans: list[tuple[int, int]] = []
    pos = 0
    for index, token in enumerate(argv):
        spans.append((pos, pos + len(token)))
        pos += len(token) + (0 if index == len(argv) - 1 else 1)
    starts = {start for start, _ in spans}
    ends = {end for _, end in spans}
    return any(
        match.start() in starts and match.end() in ends
        for pattern in patterns
        for match in pattern.finditer(line)
    )


def _segment_is_verify(argv: list[str], patterns: Sequence[re.Pattern[str]]) -> bool:
    if not argv or Path(argv[0]).name in _NON_EXECUTING:
        return False
    return _pattern_matches_argv(patterns, argv)


def command_args_prove_verification(
    args: dict[str, object], patterns: Sequence[re.Pattern[str]]
) -> bool:
    """True when this ``run_command`` exiting 0 means a verify command exited 0.

    Shared by the gate and the compaction trailer so a masked ``pytest || true`` cannot
    count as a passing run in one place and a failure in the other.
    """
    try:
        binary, rest = parse_run_command(args)
    except ValueError:
        return False
    if _argv_masks_exit([binary, *rest]):
        return False
    try:
        segments = shell_script_segments(args)
    except (ValueError, UnscreenableScript):
        return False
    for segment in segments:
        try:
            argv = shlex.split(segment)
        except ValueError:
            return False
        if _segment_is_verify(argv, patterns):
            return True
    return False


def last_proven_verification(
    records: Sequence[CommandRecord], patterns: Sequence[str]
) -> CommandRecord | None:
    """Newest recorded command whose exit code is the verify command's own exit code."""
    if not patterns:
        return None
    try:
        compiled = [re.compile(pattern) for pattern in patterns]
    except re.error:
        logger.warning("verification command pattern failed to compile", exc_info=True)
        return None
    for record in reversed(records):
        if command_args_prove_verification({"command": record.command}, compiled):
            return record
    return None


class VerificationGate:
    """Holds back and, once, refuses a final answer that follows unverified edits."""

    def __init__(self, config: VerificationGateConfig) -> None:
        self._max_rejections = config.max_rejections
        self._verify = [re.compile(p) for p in config.verify_commands]
        self._by_thread: OrderedDict[str, _MessageState] = OrderedDict()

    def register(self, manager: HookManager) -> None:
        manager.register(HookEvent.USER_MESSAGE, self.on_user_message)
        manager.register(HookEvent.POST_TOOL, self.on_post_tool)
        manager.register(HookEvent.BEFORE_PROVIDER_REQUEST, self.on_before_provider)
        manager.register(HookEvent.PRE_FINAL_ANSWER, self.on_pre_final_answer)

    def _put(self, thread_id: str, state: _MessageState) -> _MessageState:
        self._by_thread[thread_id] = state
        self._by_thread.move_to_end(thread_id)
        while len(self._by_thread) > _THREAD_STATE_CAP:
            self._by_thread.popitem(last=False)
        return state

    def _state(self, thread_id: str) -> _MessageState:
        state = self._by_thread.get(thread_id)
        if state is None:
            return self._put(thread_id, _MessageState())
        self._by_thread.move_to_end(thread_id)
        return state

    async def on_user_message(self, payload: HookPayload) -> None:
        self._put(payload.thread_id, _MessageState())

    async def on_post_tool(self, payload: HookPayload) -> None:
        try:
            self._observe(payload)
        except Exception:
            logger.warning("verification gate post_tool failed", exc_info=True)

    def _observe(self, payload: HookPayload) -> None:
        if payload.tool_error:
            return
        state = self._state(payload.thread_id)
        name = payload.tool_name or ""
        if name == "todo_list":
            state.todo_touched = True
        elif name in WRITE_TOOLS:
            state.wrote = True
            for path in _changed_paths(payload):
                state.unverified[path] = None
        elif name == "run_command" and self._is_verify_command(payload):
            result = _tool_result_json(payload)
            # Anything but an explicit foreground ``ok: true`` is not evidence. A background
            # start reports ok because the job was spawned, not because the check passed.
            if (
                result is not None
                and result.get("ok") is True
                and result.get("background") is not True
            ):
                state.unverified.clear()

    def _is_verify_command(self, payload: HookPayload) -> bool:
        args = payload.tool_args or {}
        if args.get("background") is True:
            return False
        return command_args_prove_verification(args, self._verify)

    @staticmethod
    def _open_todos(payload: HookPayload, state: _MessageState) -> list[str]:
        """Open todo texts (pending or in progress).

        Only when this message edited files or touched the list.

        Todos left over from an earlier task must not nag every unrelated answer.
        """
        store = payload.ctx.todo_store
        if store is None or not (state.wrote or state.todo_touched):
            return []
        return [item.text for item in store.items if item.status != "done"]

    def _problems(self, payload: HookPayload) -> tuple[list[str], list[str]]:
        state = self._state(payload.thread_id)
        return list(state.unverified), self._open_todos(payload, state)

    async def on_before_provider(self, payload: HookPayload) -> None:
        state = self._state(payload.thread_id)
        if state.rejections >= self._max_rejections:
            return
        changed, todos = self._problems(payload)
        if changed or todos:
            payload.hold_assistant_text = True

    async def on_pre_final_answer(self, payload: HookPayload) -> None:
        state = self._state(payload.thread_id)
        if state.rejections >= self._max_rejections:
            return
        changed, todos = self._problems(payload)
        parts: list[str] = []
        if changed:
            noun = "file" if len(changed) == 1 else "files"
            parts.append(
                f"You changed {len(changed)} {noun} ({_preview(changed)}) since the last passing "
                "verification command. Run the project's test/lint command now, or state in your "
                "answer that you could not verify and why."
            )
        if todos:
            parts.append(
                f"Open todo items: {_preview(todos)}. Finish them, or mark them done and say why."
            )
        if not parts:
            return
        state.rejections += 1
        payload.reject_final_answer = "[Harness] " + " ".join(parts)
