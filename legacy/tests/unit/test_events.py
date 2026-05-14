from __future__ import annotations

import pytest

from monkeybot.core.events import (
    AgentEvent,
    ApprovalRequest,
    ApprovalResponse,
    AssistantDelta,
    ErrorEvent,
    SubagentCompleted,
    SubagentStarted,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
    UserMessage,
    event_from_json,
    event_to_json,
)


def _roundtrip(event: AgentEvent) -> AgentEvent:
    return event_from_json(event_to_json(event))


def test_user_message_roundtrip():
    e = UserMessage(content="hello", user_id="u1")
    assert _roundtrip(e) == e


def test_assistant_delta_roundtrip():
    e = AssistantDelta(text="some chunk")
    assert _roundtrip(e) == e


def test_tool_call_started_roundtrip():
    e = ToolCallStarted(call_id="c1", tool_name="bash", args={"cmd": "ls"})
    assert _roundtrip(e) == e


def test_tool_call_result_roundtrip():
    e = ToolCallResult(
        call_id="c1",
        tool_name="bash",
        result="output",
        error=None,
        duration_ms=42,
    )
    assert _roundtrip(e) == e


def test_approval_request_roundtrip():
    e = ApprovalRequest(
        call_id="c2",
        tool_name="deploy",
        args={"env": "prod"},
        reason="production deploy",
    )
    assert _roundtrip(e) == e


def test_approval_response_roundtrip():
    e = ApprovalResponse(call_id="c2", approved=True, approver_id="admin")
    assert _roundtrip(e) == e


def test_subagent_started_roundtrip():
    e = SubagentStarted(run_id="r1", script="run.py", parent_run_id="p0")
    assert _roundtrip(e) == e


def test_subagent_completed_roundtrip():
    e = SubagentCompleted(run_id="r1", scratch_dir="/tmp/scratch")
    assert _roundtrip(e) == e


def test_turn_complete_roundtrip():
    e = TurnComplete(
        run_id="r1",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.001,
        duration_ms=500,
    )
    assert _roundtrip(e) == e


def test_error_event_roundtrip():
    e = ErrorEvent(message="something broke", recoverable=False)
    assert _roundtrip(e) == e


def test_unknown_kind_raises():
    with pytest.raises(ValueError, match="Unknown event kind"):
        event_from_json('{"kind": "bogus"}')


def test_user_message_default_timestamp_is_int():
    e = UserMessage(content="hi")
    assert isinstance(e.timestamp, int)
    assert e.timestamp > 0
