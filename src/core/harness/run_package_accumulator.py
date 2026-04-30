"""Mutable per-run scratch for assembling a frozen :class:`~.runpackage.RunPackage`.

Frames are stacked in a :class:`contextvars.ContextVar` so nested ``task`` calls
build a tree of ``subagent_runs``. The root frame is opened in
:class:`~.compiled_agent.CompiledAgent.ainvoke` and closed when the run ends.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import ExitStack
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from .errors import RecursionBudgetExceeded
from .events import EventKind, HarnessEvent, Principal, VersionTriple
from .runpackage import ApprovalRecord, RunPackage, TokenAccounting, ToolCallRecord

_FRAMES: ContextVar[tuple[_ScratchFrame, ...]] = ContextVar("harness_run_pkg_frames", default=())


@dataclass
class _ScratchFrame:
    run_id: str
    session_id: str
    principal: Principal
    versions: VersionTriple
    started_at: datetime
    inputs: list[dict[str, Any]]
    parent_run_id: str | None = None
    subagent_type: str | None = None
    parent_tool_call_id: str | None = None
    subagent_runs: list[RunPackage] = field(default_factory=list)
    token_trace: list[TokenAccounting] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    context_events: list[HarnessEvent] = field(default_factory=list)
    approvals: list[ApprovalRecord] = field(default_factory=list)
    pending_tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)


_RECURSION_EXIT_STACKS: ContextVar[tuple[ExitStack, ...]] = ContextVar("harness_recursion_exit_stacks", default=())


def enter_subagent_recursion_depth(mw: Any) -> None:
    """Push one ``SubagentRecursionMW.enter()`` frame (supports nested ``task`` calls)."""
    stack = ExitStack()
    stack.enter_context(mw.enter())
    cur = _RECURSION_EXIT_STACKS.get()
    _RECURSION_EXIT_STACKS.set(cur + (stack,))


def exit_subagent_recursion_depth() -> None:
    """Pop the innermost recursion frame opened by :func:`enter_subagent_recursion_depth`."""
    cur = _RECURSION_EXIT_STACKS.get()
    if not cur:
        return
    stack = cur[-1]
    stack.close()
    _RECURSION_EXIT_STACKS.set(cur[:-1])


class RunPackageAccumulator:
    """Owns deferred bus events (sync tool path) and exposes frame stack helpers."""

    def __init__(self) -> None:
        self._deferred_events: list[HarnessEvent] = []
        self._frames_token: Token[tuple[_ScratchFrame, ...]] | None = None

    def defer_event(self, event: HarnessEvent) -> None:
        """Queue an event when ``asyncio.get_running_loop()`` is unavailable (sync ``task``)."""
        self._deferred_events.append(event)

    async def flush_deferred_events(self, bus: Any) -> None:
        """Publish events queued from sync subagent paths."""
        while self._deferred_events:
            ev = self._deferred_events.pop(0)
            await bus.publish(ev)

    def begin_root(
        self,
        run_id: str,
        session_id: str,
        principal: Principal,
        versions: VersionTriple,
        started_at: datetime,
        inputs: list[dict[str, Any]],
    ) -> None:
        self._deferred_events.clear()
        _FRAMES.set(())
        while _RECURSION_EXIT_STACKS.get():
            exit_subagent_recursion_depth()
        frame = _ScratchFrame(
            run_id=run_id,
            session_id=session_id,
            principal=principal,
            versions=versions,
            started_at=started_at,
            inputs=list(inputs),
            parent_run_id=None,
        )
        self._frames_token = _FRAMES.set((frame,))

    def complete_root(
        self,
        outputs: list[dict[str, Any]],
        outcome: Literal["pass", "fail", "pass-with-warnings", "escalated"],
        ended_at: datetime,
    ) -> RunPackage:
        while _RECURSION_EXIT_STACKS.get():
            exit_subagent_recursion_depth()
        stack = _FRAMES.get()
        if len(stack) != 1:
            msg = "RunPackageAccumulator.complete_root: expected exactly one frame"
            raise RuntimeError(msg)
        root = stack[0]
        if self._frames_token is not None:
            _FRAMES.reset(self._frames_token)
            self._frames_token = None
        else:
            _FRAMES.set(())
        return _freeze_frame(root, outputs, outcome, ended_at)

    def push_child(
        self,
        *,
        child_run_id: str,
        session_id: str,
        principal: Principal,
        versions: VersionTriple,
        started_at: datetime,
        task_description: str,
        subagent_type: str,
        parent_run_id: str,
        parent_tool_call_id: str | None,
    ) -> None:
        stack = _FRAMES.get()
        if not stack:
            msg = "RunPackageAccumulator.push_child: no active root frame"
            raise RuntimeError(msg)
        child = _ScratchFrame(
            run_id=child_run_id,
            session_id=session_id,
            principal=principal,
            versions=versions,
            started_at=started_at,
            inputs=[{"role": "user", "content": task_description}],
            parent_run_id=parent_run_id,
            subagent_type=subagent_type,
            parent_tool_call_id=parent_tool_call_id,
        )
        _FRAMES.set(stack + (child,))

    def pop_child_to_run_package(
        self,
        outputs: list[dict[str, Any]],
        outcome: Literal["pass", "fail", "pass-with-warnings", "escalated"],
        ended_at: datetime,
        *,
        extensions: dict[str, Any] | None = None,
    ) -> RunPackage:
        stack = _FRAMES.get()
        if len(stack) < 2:
            msg = "RunPackageAccumulator.pop_child_to_run_package: no child frame"
            raise RuntimeError(msg)
        child = stack[-1]
        _FRAMES.set(stack[:-1])
        parent = stack[-2]
        pkg = _freeze_frame(child, outputs, outcome, ended_at, extensions=extensions)
        parent.subagent_runs.append(pkg)
        return pkg

    def current_frame(self) -> _ScratchFrame | None:
        stack = _FRAMES.get()
        return stack[-1] if stack else None


def _freeze_frame(
    frame: _ScratchFrame,
    outputs: list[dict[str, Any]],
    outcome: Literal["pass", "fail", "pass-with-warnings", "escalated"],
    ended_at: datetime,
    extensions: dict[str, Any] | None = None,
) -> RunPackage:
    ext = dict(extensions) if extensions else {}
    return RunPackage(
        run_id=frame.run_id,
        session_id=frame.session_id,
        principal=frame.principal,
        versions=frame.versions,
        started_at=frame.started_at,
        ended_at=ended_at,
        inputs=list(frame.inputs),
        outputs=list(outputs),
        token_trace=list(frame.token_trace),
        tool_calls=list(frame.tool_calls),
        subagent_runs=list(frame.subagent_runs),
        context_events=list(frame.context_events),
        approvals=list(frame.approvals),
        outcome=outcome,
        extensions=ext,
    )


def new_subagent_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


def messages_to_output_dicts(messages: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, dict):
            out.append(dict(m))
        else:
            role = getattr(m, "type", None) or getattr(m, "role", "assistant")
            content = getattr(m, "content", str(m))
            out.append({"role": str(role), "content": content})
    return out


_SUBAGENT_ATTEMPTS: ContextVar[list[dict[str, Any]] | None] = ContextVar("harness_subagent_attempts", default=None)


def subagent_attempts_token() -> Token[list[dict[str, Any]] | None] | None:
    """Begin tracking retry attempts for the current ``task`` invocation (call reset)."""
    return _SUBAGENT_ATTEMPTS.set([])


def subagent_attempts_reset(token: Token[list[dict[str, Any]] | None] | None) -> list[dict[str, Any]]:
    if token is None:
        return []
    bucket = list(_SUBAGENT_ATTEMPTS.get() or [])
    _SUBAGENT_ATTEMPTS.reset(token)
    return bucket


def record_subagent_attempt(attempt: int, error: str, error_kind: str = "unexpected") -> None:
    bucket = _SUBAGENT_ATTEMPTS.get()
    if bucket is not None:
        bucket.append({"attempt": attempt, "error_kind": error_kind, "error": error})


def schedule_harness_event(bus: Any, accumulator: RunPackageAccumulator | None, event: HarnessEvent) -> None:
    """Publish immediately when a loop is running; otherwise defer for ``flush_deferred_events``."""
    if bus is None:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        if accumulator is not None:
            accumulator.defer_event(event)
        return

    async def _pub() -> None:
        await bus.publish(event)

    asyncio.create_task(_pub())


class SubagentInvocationHooks:
    """Per-root-run hooks invoked from the patched ``task`` / ``atask`` implementation."""

    def __init__(
        self,
        accumulator: RunPackageAccumulator,
        event_bus: Any,
        recursion_mw: Any,
        *,
        versions: VersionTriple,
    ) -> None:
        self._accum = accumulator
        self._bus = event_bus
        self._recursion_mw = recursion_mw
        self._versions = versions

    def finish_success(self, result: dict[str, Any], attempts: list[dict[str, Any]] | None) -> None:
        messages = list(result.get("messages") or [])
        self.finish_subagent(
            messages=messages,
            outcome="pass",
            attempts=attempts,
        )

    def finish_failure(
        self,
        *,
        messages: list[Any],
        outcome: Literal["pass", "fail", "pass-with-warnings", "escalated"] = "fail",
        attempts: list[dict[str, Any]] | None = None,
    ) -> None:
        self.finish_subagent(messages=messages, outcome=outcome, attempts=attempts)

    def start_subagent(
        self,
        *,
        subagent_type: str,
        description: str,
        session_id: str,
        principal: Principal,
        parent_run_id: str,
        parent_tool_call_id: str | None,
    ) -> str:
        cid = new_subagent_run_id()
        started = datetime.now(UTC)
        self._accum.push_child(
            child_run_id=cid,
            session_id=session_id,
            principal=principal,
            versions=self._versions,
            started_at=started,
            task_description=description,
            subagent_type=subagent_type,
            parent_run_id=parent_run_id,
            parent_tool_call_id=parent_tool_call_id,
        )
        schedule_harness_event(
            self._bus,
            self._accum,
            HarnessEvent(
                run_id=cid,
                session_id=session_id,
                parent_run_id=parent_run_id,
                principal=principal,
                versions=self._versions,
                ts=started,
                kind=EventKind.SUBAGENT_SPAWN,
                payload={
                    "subagent_type": subagent_type,
                    "parent_run_id": parent_run_id,
                    "parent_tool_call_id": parent_tool_call_id,
                },
            ),
        )
        try:
            enter_subagent_recursion_depth(self._recursion_mw)
        except RecursionBudgetExceeded:
            ended = datetime.now(UTC)
            self._accum.pop_child_to_run_package(
                [],
                "fail",
                ended,
                extensions={"error_kind": "recursion", "subagent_type": subagent_type},
            )
            schedule_harness_event(
                self._bus,
                self._accum,
                HarnessEvent(
                    run_id=cid,
                    session_id=session_id,
                    parent_run_id=parent_run_id,
                    principal=principal,
                    versions=self._versions,
                    ts=ended,
                    kind=EventKind.SUBAGENT_RETURN,
                    payload={"outcome": "fail", "error_kind": "recursion", "subagent_type": subagent_type},
                ),
            )
            raise
        return cid

    def finish_subagent(
        self,
        *,
        messages: list[Any],
        outcome: Literal["pass", "fail", "pass-with-warnings", "escalated"],
        attempts: list[dict[str, Any]] | None,
    ) -> None:
        fr = self._accum.current_frame()
        if fr is None or fr.subagent_type is None:
            return
        cid = fr.run_id
        session_id = fr.session_id
        principal = fr.principal
        parent_run_id = fr.parent_run_id or fr.run_id
        subatype = fr.subagent_type
        ended = datetime.now(UTC)
        exit_subagent_recursion_depth()
        outs = messages_to_output_dicts(messages)
        ext: dict[str, Any] = {}
        if attempts:
            ext["attempts"] = attempts
        self._accum.pop_child_to_run_package(outs, outcome, ended, extensions=ext or None)
        schedule_harness_event(
            self._bus,
            self._accum,
            HarnessEvent(
                run_id=cid,
                session_id=session_id,
                parent_run_id=parent_run_id,
                principal=principal,
                versions=self._versions,
                ts=ended,
                kind=EventKind.SUBAGENT_RETURN,
                payload={"outcome": outcome, "subagent_type": subatype},
            ),
        )

    def record_invalid_subagent_delegation(
        self,
        *,
        subagent_type: str,
        description: str,
        message: str,
        session_id: str,
        principal: Principal,
        parent_run_id: str,
        parent_tool_call_id: str | None,
    ) -> None:
        """No runnable invoked (unknown type); still append a child RunPackage."""
        cid = new_subagent_run_id()
        started = datetime.now(UTC)
        self._accum.push_child(
            child_run_id=cid,
            session_id=session_id,
            principal=principal,
            versions=self._versions,
            started_at=started,
            task_description=description,
            subagent_type=subagent_type,
            parent_run_id=parent_run_id,
            parent_tool_call_id=parent_tool_call_id,
        )
        ended = datetime.now(UTC)
        self._accum.pop_child_to_run_package(
            [{"role": "assistant", "content": message}],
            "pass-with-warnings",
            ended,
            extensions={"error_kind": "unknown_subagent", "subagent_type": subagent_type},
        )


ACTIVE_SUBAGENT_HOOKS: ContextVar[SubagentInvocationHooks | None] = ContextVar("harness_active_subagent_hooks", default=None)


def set_active_subagent_hooks(hooks: SubagentInvocationHooks | None) -> Token[SubagentInvocationHooks | None]:
    return ACTIVE_SUBAGENT_HOOKS.set(hooks)


def reset_active_subagent_hooks(token: Token[SubagentInvocationHooks | None]) -> None:
    ACTIVE_SUBAGENT_HOOKS.reset(token)


def event_parent_run_id_for_spawn(hooks: SubagentInvocationHooks | None, root_run_id: str) -> str:
    """Harness ``parent_run_id`` for nested ``task``: immediate delegator run, else root."""
    if hooks is None:
        return root_run_id
    fr = hooks._accum.current_frame()
    if fr is not None and fr.subagent_type is not None:
        return fr.run_id
    return root_run_id
