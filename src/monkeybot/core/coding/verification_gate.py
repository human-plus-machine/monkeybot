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
from collections import OrderedDict
from dataclasses import dataclass, field

from monkeybot.core.config.settings import VerificationGateConfig
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.tools.inspector import norm_run_command_line, shell_script_segments
from monkeybot.core.verifier.match import WRITE_TOOLS, path_args

logger = logging.getLogger(__name__)

_THREAD_STATE_CAP = 256
_MAX_LISTED = 5
# Commands that read, list or move files never run a project's checks, even when a file or
# string they handle is called "pytest" or "ruff.toml".
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
    }
)


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

    def _state(self, thread_id: str) -> _MessageState:
        state = self._by_thread.get(thread_id)
        if state is None:
            state = _MessageState()
            self._by_thread[thread_id] = state
            while len(self._by_thread) > _THREAD_STATE_CAP:
                self._by_thread.popitem(last=False)
        else:
            self._by_thread.move_to_end(thread_id)
        return state

    async def on_user_message(self, payload: HookPayload) -> None:
        self._by_thread[payload.thread_id] = _MessageState()
        self._by_thread.move_to_end(payload.thread_id)

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
            # Anything but an explicit ``ok: true`` (non-JSON, failure, timeout) is not evidence.
            if result is not None and result.get("ok") is True:
                state.unverified.clear()

    def _is_verify_command(self, payload: HookPayload) -> bool:
        args = payload.tool_args or {}
        try:
            lines = shell_script_segments(args) or [norm_run_command_line(args) or ""]
        except ValueError:
            return False
        for line in lines:
            first = line.split(" ", 1)[0].rsplit("/", 1)[-1]
            if first in _NON_EXECUTING:
                continue
            if any(rx.search(line) for rx in self._verify):
                return True
        return False

    @staticmethod
    def _open_todos(payload: HookPayload, state: _MessageState) -> list[str]:
        """Pending todo texts, but only when this message edited files or touched the list.

        Todos left over from an earlier task must not nag every unrelated answer.
        """
        store = payload.ctx.todo_store
        if store is None or not (state.wrote or state.todo_touched):
            return []
        return [item.text for item in store.items if item.status == "pending"]

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
