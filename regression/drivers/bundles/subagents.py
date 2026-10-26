"""Subagent event publisher, child ledger, and cancel held until the child runs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

from monkeybot.core.hooks import HookManager
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.verifier.ledger import GoalLedger
from regression.drivers.bundles.protocol import Bundle, RunCtx
from regression.drivers.bundles.verifier import _close_ledger, _register_goal_ledger
from regression.schema.steps import CancelStep, StepSchedule
from testkit.verifier import ScriptedClassifier


def _script_spawns_task(scenario: object) -> bool:
    """True when the script asks the real executor to run ``task``."""
    script = getattr(scenario, "script", None)
    if script is None:
        return False
    for turn in script:
        for event in turn:
            if getattr(event, "type", None) != "tool_call":
                continue
            if getattr(event, "name", None) == "task":
                return True
    return False


def _open_task_goal_ledger(
    manager: HookManager,
    backend: SQLiteStorageBackend,
    env: Mapping[str, str],
) -> GoalLedger | None:
    """Ledger for a task spawn the verifier bundle did not already install.

    Scripted classifier rows win. With none, the user message is the active
    goal so ``format_subagent_context`` has a parent ledger to forward.
    """
    store = backend.goal_ledger()
    if store is None:
        return None
    classifier = ScriptedClassifier.from_env(env) or ScriptedClassifier([{"intent": "new_goal"}])
    ledger = GoalLedger(store, classifier)
    _register_goal_ledger(manager, ledger)
    return ledger


def _task_cancel_step(scenario: object) -> bool:
    """True when cancel is tied to the task tool's ``ToolCallStarted``."""
    for step in getattr(scenario, "steps", ()):
        if not isinstance(step, CancelStep):
            continue
        if step.when.event != "ToolCallStarted":
            continue
        if step.when.name == "task":
            return True
    return False


class _HeldCancel(asyncio.Event):
    """Cancel flag that can ignore ``set`` until the child is actually running.

    A ``task`` chunk checks the flag after ``ToolCallStarted`` and before
    ``execute``. Setting it on that event makes the chunk refuse to start.
    """

    def __init__(self) -> None:
        super().__init__()
        self.hold = False
        self._queued = False

    def set(self) -> None:
        if self.hold:
            self._queued = True
            return
        super().set()

    def release(self) -> None:
        self.hold = False
        if self._queued:
            super().set()


async def _release_when_child_running(workspace: Path, gate: _HeldCancel) -> None:
    """Set cancel once the hang child has written ``subagent.pgid``."""
    marker = workspace / "subagent.pgid"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10.0
    while loop.time() < deadline:
        await asyncio.sleep(0.01)
        if marker.is_file():
            gate.release()
            return
    gate.release()


class _SubagentPublisher:
    """Append nested subagent events to the run the assertions read."""

    def __init__(
        self,
        events: list[object],
        schedule: StepSchedule,
        gate: _HeldCancel | None,
        workspace: Path | None,
    ) -> None:
        self._events = events
        self._schedule = schedule
        self._gate = gate
        self._workspace = workspace
        self.watch: asyncio.Task[None] | None = None

    async def publish_event(self, event: object) -> None:
        self._events.extend(self._schedule.observe(event))
        self._events.append(event)
        if (
            self.watch is None
            and self._gate is not None
            and self._gate.hold
            and self._workspace is not None
            and getattr(event, "kind", None) == "SubagentStarted"
        ):
            self.watch = asyncio.create_task(
                _release_when_child_running(self._workspace, self._gate)
            )


def _subagent_run_dir(workspace: Path | None) -> Path | None:
    """Scratch directory that holds ``progress.jsonl`` / ``output.json``."""
    if workspace is None:
        return None
    root = workspace / ".monkeybot" / "subagent-runs"
    if not root.is_dir():
        return None
    dirs = [path for path in root.iterdir() if path.is_dir()]
    if not dirs:
        return None

    def rank(path: Path) -> tuple[int, int, float]:
        output = 1 if (path / "output.json").is_file() else 0
        progress = path / "progress.jsonl"
        lines = 0
        if progress.is_file():
            try:
                text = progress.read_text(encoding="utf-8")
            except OSError:
                text = ""
            lines = sum(1 for line in text.splitlines() if line.strip())
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0.0
        return (output, lines, modified)

    return max(dirs, key=rank)


class _SubagentBundle(Bundle):
    order = 80
    after_eval = True

    def register(self, manager: HookManager, ctx: RunCtx) -> None:
        if (
            ctx.goal_ledger is None
            and ctx.backend is not None
            and _script_spawns_task(ctx.scenario)
        ):
            ctx.task_ledger = _open_task_goal_ledger(manager, ctx.backend, ctx.env)
            ctx.goal_ledger = ctx.task_ledger

    async def close(self, ctx: RunCtx) -> None:
        if ctx.task_ledger is None:
            return
        await _close_ledger(ctx.task_ledger)
        ctx.task_ledger = None


BUNDLE = _SubagentBundle()
