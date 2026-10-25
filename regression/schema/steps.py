"""Mid-run scenario steps.

Models only. P1-03 runs the loop-driver steps. ``disconnect_sse`` and
``reconnect_sse`` belong to the gateway driver (P2-01). ``client_text`` and
``interrupt`` belong to the realtime driver (P2-05).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

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


class AdvanceClockStep(_Step):
    action: Literal["advance_clock"]
    ms: int


class ReloadConfigStep(_Step):
    action: Literal["reload_config"]
    config: dict[str, Any] | None = None


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
    | DisconnectSseStep
    | ReconnectSseStep
    | ClientTextStep
    | InterruptStep,
    Field(discriminator="action"),
]


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
    "ReloadConfigStep",
    "SteerStep",
    "Step",
    "TouchFileStep",
]
