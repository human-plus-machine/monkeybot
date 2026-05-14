"""
Typed AgentEvent stream.
loop.run() is an AsyncIterator[AgentEvent].
"""
from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import dataclass, field
from typing import Literal, cast


@dataclass
class UserMessage:
    """A message from the user.

    Attributes:
        kind: Event discriminator.
        content: Message text.
        user_id: Optional user identifier.
        timestamp: Unix milliseconds at creation.
    """

    kind: Literal["user_message"] = "user_message"
    content: str = ""
    user_id: str | None = None
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class AssistantDelta:
    """Streaming text chunk from the model.

    Attributes:
        kind: Event discriminator.
        text: Partial text content.
    """

    kind: Literal["assistant_delta"] = "assistant_delta"
    text: str = ""


@dataclass
class ToolCallStarted:
    """The model has requested a tool call.

    Attributes:
        kind: Event discriminator.
        call_id: Unique identifier for this tool call.
        tool_name: Name of the tool being called.
        args: Arguments passed to the tool.
    """

    kind: Literal["tool_call_started"] = "tool_call_started"
    call_id: str = ""
    tool_name: str = ""
    args: dict = field(default_factory=dict)  # type: ignore[type-arg]


@dataclass
class ToolCallResult:
    """Result of a completed tool call.

    Attributes:
        kind: Event discriminator.
        call_id: Matches the ToolCallStarted call_id.
        tool_name: Name of the tool that was called.
        result: Tool output as a string.
        error: Error message if the call failed.
        duration_ms: Execution time in milliseconds.
    """

    kind: Literal["tool_call_result"] = "tool_call_result"
    call_id: str = ""
    tool_name: str = ""
    result: str = ""
    error: str | None = None
    duration_ms: int = 0


@dataclass
class ApprovalRequest:
    """HITL gate — loop pauses until gateway responds.

    Attributes:
        kind: Event discriminator.
        call_id: Unique identifier for this approval request.
        tool_name: Name of the tool requiring approval.
        args: Arguments that will be passed to the tool.
        reason: Human-readable reason for requesting approval.
    """

    kind: Literal["approval_request"] = "approval_request"
    call_id: str = ""
    tool_name: str = ""
    args: dict = field(default_factory=dict)  # type: ignore[type-arg]
    reason: str = ""


@dataclass
class ApprovalResponse:
    """Response to an approval request.

    Attributes:
        kind: Event discriminator.
        call_id: Matches the ApprovalRequest call_id.
        approved: Whether the tool call was approved.
        approver_id: Optional identifier of the approver.
    """

    kind: Literal["approval_response"] = "approval_response"
    call_id: str = ""
    approved: bool = False
    approver_id: str | None = None


@dataclass
class SubagentStarted:
    """A subagent process has been spawned.

    Attributes:
        kind: Event discriminator.
        run_id: Unique identifier for the subagent run.
        script: Script or entry point being executed.
        parent_run_id: Optional parent run identifier.
    """

    kind: Literal["subagent_started"] = "subagent_started"
    run_id: str = ""
    script: str = ""
    parent_run_id: str | None = None


@dataclass
class SubagentCompleted:
    """A subagent process has finished.

    Attributes:
        kind: Event discriminator.
        run_id: Unique identifier for the subagent run.
        scratch_dir: Path to the subagent's scratch directory.
    """

    kind: Literal["subagent_completed"] = "subagent_completed"
    run_id: str = ""
    scratch_dir: str = ""


@dataclass
class TurnComplete:
    """The agent turn has finished.

    Attributes:
        kind: Event discriminator.
        run_id: Unique identifier for the turn.
        input_tokens: Number of input tokens consumed.
        output_tokens: Number of output tokens generated.
        cached_tokens: Number of tokens served from cache.
        cost_usd: Estimated cost in US dollars.
        duration_ms: Total turn duration in milliseconds.
    """

    kind: Literal["turn_complete"] = "turn_complete"
    run_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0


@dataclass
class ErrorEvent:
    """An error occurred during the turn.

    Attributes:
        kind: Event discriminator.
        message: Human-readable error description.
        recoverable: Whether the loop can continue after this error.
    """

    kind: Literal["error"] = "error"
    message: str = ""
    recoverable: bool = True


AgentEvent = (
    UserMessage
    | AssistantDelta
    | ToolCallStarted
    | ToolCallResult
    | ApprovalRequest
    | ApprovalResponse
    | SubagentStarted
    | SubagentCompleted
    | TurnComplete
    | ErrorEvent
)

_KIND_MAP: dict[str, type] = {
    "user_message": UserMessage,
    "assistant_delta": AssistantDelta,
    "tool_call_started": ToolCallStarted,
    "tool_call_result": ToolCallResult,
    "approval_request": ApprovalRequest,
    "approval_response": ApprovalResponse,
    "subagent_started": SubagentStarted,
    "subagent_completed": SubagentCompleted,
    "turn_complete": TurnComplete,
    "error": ErrorEvent,
}


def event_to_json(event: AgentEvent) -> str:
    """Serialize an AgentEvent to a JSON string.

    Args:
        event: The event to serialize.

    Returns:
        A JSON string representation of the event.
    """
    return json.dumps(dataclasses.asdict(event))


def event_from_json(line: str) -> AgentEvent:
    """Deserialize a JSON string to an AgentEvent.

    Args:
        line: A JSON string produced by event_to_json.

    Returns:
        The deserialized AgentEvent.

    Raises:
        ValueError: If the kind field is unknown or missing.
    """
    data: dict = json.loads(line)  # type: ignore[type-arg]
    kind = data.get("kind")
    cls = _KIND_MAP.get(kind)  # type: ignore[arg-type]
    if not cls:
        raise ValueError(f"Unknown event kind: {kind}")
    return cast(AgentEvent, cls(**{k: v for k, v in data.items() if k != "kind"}))
