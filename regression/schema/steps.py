"""Mid-run scenario steps.

Models and loop-driver execution. ``disconnect_sse`` and ``reconnect_sse``
belong to the gateway driver (P2-01). ``client_text`` and ``interrupt`` belong
to the realtime driver (P2-05). The loop driver rejects those four.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from monkeybot.core.llm.provider import Message
from monkeybot.core.runtime.events import ConfigReloaded, QueuedInputAccepted
from monkeybot.core.runtime.history_rewrite import HistoryRewriteError, prefix_end_for
from monkeybot.core.runtime.input_admission import InputAdmission
from monkeybot.core.types.content_blocks import Text, ToolResponse
from regression.schema.assertions import event_field, event_type
from testkit.clock import FakeClock
from testkit.pending_bus import PresetPendingBus

# ``when``, not ``on``: YAML 1.1 treats the bare word ``on`` as a boolean.


class EventPredicate(BaseModel):
    """Fire a step on the nth event of a kind, optionally for one tool call."""

    model_config = ConfigDict(extra="forbid")

    event: str = Field(min_length=1)
    nth: int = Field(default=1, ge=1)
    name: str | None = None
    call_id: str | None = None


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: EventPredicate


class CancelStep(_Step):
    action: Literal["cancel"]


class SteerStep(_Step):
    action: Literal["steer"]
    text: str = Field(min_length=1)


class QueueFollowUpStep(_Step):
    action: Literal["queue_follow_up"]
    text: str = Field(min_length=1)
    request_id: str | None = None


class PromoteFollowUpStep(_Step):
    action: Literal["promote_follow_up"]
    request_id: str = Field(min_length=1)


class AnswerAskUserStep(_Step):
    action: Literal["answer_ask_user"]
    answer: str
    call_id: str | None = None


class ConfirmToolStep(_Step):
    action: Literal["confirm_tool"]
    decision: Literal["approve", "deny", "always"]
    call_id: str | None = None
    reason: str | None = None


class TouchFileStep(_Step):
    """Change a workspace file's mtime and/or size between events.

    Omitted ``mtime`` means the runner sets mtime when it executes the step.
    Omitted ``size`` means the runner leaves the size alone. ``mtime`` is epoch
    seconds; ``size`` is the new byte length.
    """

    action: Literal["touch_file"]
    path: str = Field(min_length=1)
    mtime: float | None = None
    size: int | None = Field(default=None, ge=0)
    fill: str | None = Field(default=None, min_length=1, max_length=1)


class AdvanceClockStep(_Step):
    action: Literal["advance_clock"]
    ms: int


class ReloadConfigStep(_Step):
    action: Literal["reload_config"]
    config: dict[str, Any] | None = None


class SetMemoryLinesStep(_Step):
    """Append lines to the next memory-index reload.

    ``refresh_memory_index`` re-reads the palace on every inner turn. Lines set
    here show up on the next reload, so a memory change can land after the
    current-request block has already settled.
    """

    action: Literal["set_memory_lines"]
    lines: list[str]


class RewindStep(_Step):
    """Drop history after the turn that contains ``through_text``.

    Same cut as a session rewind: a user-text row keeps that message and drops
    the reply after it. The running thread's history is shortened in place, so
    a background summary built for the longer transcript becomes stale.
    """

    action: Literal["rewind"]
    through_text: str = Field(min_length=1)


class DisconnectSseStep(_Step):
    action: Literal["disconnect_sse"]


class ReconnectSseStep(_Step):
    action: Literal["reconnect_sse"]
    last_event_id: str = Field(min_length=1)


class ClientTextStep(_Step):
    action: Literal["client_text"]
    text: str = Field(min_length=1)


class InterruptStep(_Step):
    action: Literal["interrupt"]


Step: TypeAlias = Annotated[
    CancelStep
    | SteerStep
    | QueueFollowUpStep
    | PromoteFollowUpStep
    | AnswerAskUserStep
    | ConfirmToolStep
    | TouchFileStep
    | AdvanceClockStep
    | ReloadConfigStep
    | SetMemoryLinesStep
    | RewindStep
    | DisconnectSseStep
    | ReconnectSseStep
    | ClientTextStep
    | InterruptStep,
    Field(discriminator="action"),
]


# Predicate names from the scenario schema, mapped onto agent-event kinds.
# ``done`` is the provider stream finishing its text (``AssistantTextEnded``),
# which can happen once per inner turn. ``TurnComplete`` is the whole user
# message and is not ``done``.
_PREDICATE_KINDS: dict[str, frozenset[str]] = {
    "text": frozenset({"AssistantDelta"}),
    "tool_result": frozenset({"ToolCallResult"}),
    "tool_input": frozenset({"ToolInputDelta"}),
    "done": frozenset({"AssistantTextEnded"}),
    "ask_user": frozenset({"AskUserRequest"}),
    "tool_confirmation": frozenset({"ToolConfirmationRequest"}),
}

_GATEWAY_ONLY = frozenset({"disconnect_sse", "reconnect_sse"})
_REALTIME_ONLY = frozenset({"client_text", "interrupt"})


class StepExecutionError(RuntimeError):
    """A loop-driver step matched and then could not run."""


@dataclass(frozen=True)
class StepFired:
    """Record that a loop-driver step ran. Inserted just before its trigger."""

    kind: Literal["StepFired"] = "StepFired"
    action: str = ""
    detail: str = ""


@dataclass(frozen=True)
class ReloadedConfig:
    """What a ``reload_config`` step built. The driver applies it on the next ``run``."""

    revision: int
    digest: str
    applied: list[str]
    context_window_tokens: int


@dataclass
class StepBindings:
    """Public objects a step may touch. The loop driver fills this in."""

    clock: FakeClock
    admission: InputAdmission | None
    cancelled: asyncio.Event | None
    bus: PresetPendingBus | None
    workspace: Path | None
    request_id: str
    reload: Callable[[Mapping[str, Any] | None], ReloadedConfig]
    history: Any = None
    thread_id: str = ""
    memory_lines: list[str] | None = None


def loop_driver_rejection(action: str) -> str | None:
    """Why the loop driver refuses ``action``, or None when it runs the step."""
    if action in _GATEWAY_ONLY:
        return (
            f"loop driver does not run {action}; "
            "disconnect_sse and reconnect_sse belong to the gateway driver (P2-01)"
        )
    if action in _REALTIME_ONLY:
        return (
            f"loop driver does not run {action}; "
            "client_text and interrupt belong to the realtime driver (P2-05)"
        )
    return None


class StepSchedule:
    """Fire each step once, on the nth matching event."""

    def __init__(self, steps: list[Step], bindings: StepBindings) -> None:
        self._pending = list(steps)
        self._bindings = bindings
        self._counts: dict[tuple[str, str, str], int] = {}

    def pending(self) -> list[Step]:
        return list(self._pending)

    def observe(self, event: object) -> list[object]:
        """Run steps whose predicate matches ``event``. Records come back first."""
        if not self._pending:
            return []
        fired: list[Step] = []
        still: list[Step] = []
        seen: set[tuple[str, str, str]] = set()
        for step in self._pending:
            if not _predicate_matches(step.when, event):
                still.append(step)
                continue
            key = (step.when.event, step.when.name or "", step.when.call_id or "")
            if key not in seen:
                self._counts[key] = self._counts.get(key, 0) + 1
                seen.add(key)
            if self._counts[key] == step.when.nth:
                fired.append(step)
            else:
                still.append(step)
        self._pending = still
        produced: list[object] = []
        for step in fired:
            produced.extend(execute_step(step, event, self._bindings))
        return produced


def execute_step(step: Step, event: object, bindings: StepBindings) -> list[object]:
    """Perform one loop-driver step. The returned events are the step's record."""
    reason = loop_driver_rejection(step.action)
    if reason is not None:
        raise StepExecutionError(reason)
    if isinstance(step, CancelStep):
        return _cancel(bindings)
    if isinstance(step, SteerStep):
        return _steer(step, bindings)
    if isinstance(step, QueueFollowUpStep):
        return _queue_follow_up(step, bindings)
    if isinstance(step, PromoteFollowUpStep):
        return _promote_follow_up(step, bindings)
    if isinstance(step, AnswerAskUserStep):
        return _answer_ask_user(step, event, bindings)
    if isinstance(step, ConfirmToolStep):
        return _confirm_tool(step, event, bindings)
    if isinstance(step, TouchFileStep):
        return _touch_file(step, bindings)
    if isinstance(step, AdvanceClockStep):
        return _advance_clock(step, bindings)
    if isinstance(step, ReloadConfigStep):
        return _reload_config(step, bindings)
    if isinstance(step, SetMemoryLinesStep):
        return _set_memory_lines(step, bindings)
    if isinstance(step, RewindStep):
        return _rewind(step, bindings)
    raise StepExecutionError(f"loop driver cannot run {step.action}")


def _cancel(bindings: StepBindings) -> list[object]:
    if bindings.cancelled is None:
        raise StepExecutionError("cancel has no cancelled event to set")
    bindings.cancelled.set()
    return [StepFired(action="cancel")]


def _steer(step: SteerStep, bindings: StepBindings) -> list[object]:
    admission = _admission(bindings)
    position = admission.enqueue_steer([Text(text=step.text)])
    return [
        StepFired(action="steer", detail=step.text),
        QueuedInputAccepted(request_id=bindings.request_id, queue="steer", position=position),
    ]


def _queue_follow_up(step: QueueFollowUpStep, bindings: StepBindings) -> list[object]:
    admission = _admission(bindings)
    request_id = step.request_id or f"follow-up-{admission.follow_up_depth + 1}"
    position = admission.enqueue_follow_up(request_id, [Text(text=step.text)])
    return [
        StepFired(action="queue_follow_up", detail=request_id),
        QueuedInputAccepted(
            request_id=request_id,
            queue="follow_up",
            position=position,
        ),
    ]


def _promote_follow_up(step: PromoteFollowUpStep, bindings: StepBindings) -> list[object]:
    admission = _admission(bindings)
    position = admission.promote_follow_up(step.request_id)
    return [
        StepFired(action="promote_follow_up", detail=step.request_id),
        QueuedInputAccepted(request_id=bindings.request_id, queue="steer", position=position),
    ]


def _answer_ask_user(
    step: AnswerAskUserStep, event: object, bindings: StepBindings
) -> list[object]:
    key = _pending_key(step.action, step.call_id, event)
    _resolve(bindings, key, {"answer": step.answer})
    return [StepFired(action="answer_ask_user", detail=key)]


def _confirm_tool(step: ConfirmToolStep, event: object, bindings: StepBindings) -> list[object]:
    key = _pending_key(step.action, step.call_id, event)
    if step.decision == "deny":
        payload: dict[str, Any] = {"approved": False, "reason": step.reason or "denied by user"}
    elif step.decision == "always":
        payload = {"approved": True, "always": True}
    else:
        payload = {"approved": True}
    _resolve(bindings, key, payload)
    return [StepFired(action="confirm_tool", detail=f"{step.decision}:{key}")]


def _row_text(message: Message) -> str:
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, Text):
            parts.append(block.text)
        elif isinstance(block, ToolResponse):
            parts.extend(piece.text for piece in block.result if isinstance(piece, Text))
    return "".join(parts)


def _set_memory_lines(step: SetMemoryLinesStep, bindings: StepBindings) -> list[object]:
    if bindings.memory_lines is None:
        raise StepExecutionError("set_memory_lines needs a memory subsystem")
    bindings.memory_lines[:] = list(step.lines)
    return [StepFired(action="set_memory_lines", detail=f"lines={len(step.lines)}")]


def _rewind(step: RewindStep, bindings: StepBindings) -> list[object]:
    history = bindings.history
    rows = getattr(history, "rows", None)
    if not isinstance(rows, list):
        raise StepExecutionError("rewind has no history")
    stamped: list[Message] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Message):
            raise StepExecutionError("rewind history rows must be messages")
        stamped.append(row if row.row_id else replace(row, row_id=f"row-{index}"))
    anchor = None
    for index, row in enumerate(stamped):
        if step.through_text in _row_text(row):
            anchor = index
    if anchor is None:
        raise StepExecutionError(f"rewind found no row containing {step.through_text!r}")
    try:
        end = prefix_end_for(stamped, anchor, "rewind")
    except HistoryRewriteError as exc:
        raise StepExecutionError(str(exc)) from exc
    history.rows = stamped[:end]
    return [StepFired(action="rewind", detail=step.through_text)]


def _touch_file(step: TouchFileStep, bindings: StepBindings) -> list[object]:
    if bindings.workspace is None:
        raise StepExecutionError("touch_file needs a workspace directory")
    target = _workspace_file(bindings.workspace, step.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = target.read_bytes() if target.exists() else b""
    if step.size is not None:
        pad = step.size - len(data)
        unit = b" " if step.fill is None else step.fill.encode("utf-8")
        if len(unit) != 1:
            raise StepExecutionError("touch_file fill must be a single byte")
        data = data + unit * pad if pad > 0 else data[: step.size]
        target.write_bytes(data)
    elif not target.exists():
        target.write_bytes(data)
    mtime = time.time() if step.mtime is None else step.mtime
    os.utime(target, (mtime, mtime))
    stat = target.stat()
    detail = f"path={step.path} size={stat.st_size} mtime={int(stat.st_mtime)}"
    return [StepFired(action="touch_file", detail=detail)]


def _advance_clock(step: AdvanceClockStep, bindings: StepBindings) -> list[object]:
    now_ms = bindings.clock.advance(step.ms)
    return [StepFired(action="advance_clock", detail=f"now_ms={now_ms}")]


def _reload_config(step: ReloadConfigStep, bindings: StepBindings) -> list[object]:
    reloaded = bindings.reload(step.config)
    return [
        StepFired(
            action="reload_config",
            detail=f"context_window={reloaded.context_window_tokens}",
        ),
        ConfigReloaded(
            request_id=bindings.request_id,
            revision=reloaded.revision,
            digest=reloaded.digest,
            applied=list(reloaded.applied),
        ),
    ]


def _admission(bindings: StepBindings) -> InputAdmission:
    if bindings.admission is None:
        raise StepExecutionError("steer and follow-up steps need an InputAdmission")
    return bindings.admission


def _resolve(bindings: StepBindings, key: str, payload: dict[str, Any]) -> None:
    if bindings.bus is None:
        raise StepExecutionError("confirm_tool and answer_ask_user need a pending bus")
    if not bindings.bus.resolve_pending(key, payload):
        raise StepExecutionError(f"pending response {key} was already completed")


def _pending_key(action: str, call_id: str | None, event: object) -> str:
    key = call_id or _event_call_id(event)
    if not key:
        raise StepExecutionError(f"{action} needs a call_id on the step or the event")
    return key


def _predicate_matches(pred: EventPredicate, event: object) -> bool:
    kind = event_type(event)
    if kind not in _PREDICATE_KINDS.get(pred.event, frozenset({pred.event})):
        return False
    if pred.name is not None and _event_name(event) != pred.name:
        return False
    return pred.call_id is None or _event_call_id(event) == pred.call_id


def _event_name(event: object) -> str | None:
    for attr in ("tool", "tool_name", "name"):
        value = event_field(event, attr)
        if isinstance(value, str) and value:
            return value
    return None


def _event_call_id(event: object) -> str | None:
    for attr in ("call_id", "tool_call_id"):
        value = event_field(event, attr)
        if isinstance(value, str) and value:
            return value
    return None


def _workspace_file(workspace: Path, raw: str) -> Path:
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise StepExecutionError(f"touch_file path must stay inside the workspace: {raw!r}")
    target = (workspace / relative).resolve()
    try:
        target.relative_to(workspace.resolve())
    except ValueError as exc:
        raise StepExecutionError(
            f"touch_file path must stay inside the workspace: {raw!r}"
        ) from exc
    return target


__all__ = [
    "AdvanceClockStep",
    "AnswerAskUserStep",
    "CancelStep",
    "ClientTextStep",
    "ConfirmToolStep",
    "DisconnectSseStep",
    "EventPredicate",
    "InterruptStep",
    "PromoteFollowUpStep",
    "QueueFollowUpStep",
    "ReconnectSseStep",
    "ReloadedConfig",
    "ReloadConfigStep",
    "Step",
    "StepBindings",
    "StepExecutionError",
    "StepFired",
    "StepSchedule",
    "SteerStep",
    "TouchFileStep",
    "execute_step",
    "loop_driver_rejection",
]
