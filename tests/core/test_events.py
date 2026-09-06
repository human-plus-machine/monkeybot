"""Tests for :mod:`monkeybot.core.runtime.events`."""

from __future__ import annotations

import json
from typing import Literal, cast, get_args

import pytest

from monkeybot.core.runtime.events import (
    ActionRequiredEvent,
    AgentEvent,
    AssistantDelta,
    AssistantTextEnded,
    AssistantTextStarted,
    ContextUsage,
    CredentialEgressBlockedEvent,
    Error,
    EventDecodeError,
    FrontendToolRequestEvent,
    GroundingEvent,
    ImageBlock,
    QueuedInputAccepted,
    RedactedThinkingBlock,
    SystemContextUpdated,
    SystemNotificationEvent,
    SystemPromptSnapshot,
    Thinking,
    ThinkingBlockComplete,
    ThinkingBlockDelta,
    ThinkingBlockStarted,
    ToolCallResult,
    ToolCallStarted,
    ToolConfirmationRequestEvent,
    ToolInputDeltaEvent,
    TurnComplete,
    UsageTotals,
    UserSteered,
    event_from_json,
    event_to_json,
)


def test_agent_event_roundtrip_thinking() -> None:
    ev = Thinking(request_id="r1")
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_assistant_delta() -> None:
    ev = AssistantDelta(request_id="r1", delta="café")
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_tool_call_started() -> None:
    ev = ToolCallStarted(
        request_id="r1",
        tool="run_command",
        label="Run",
        args={"cmd": "ls"},
        call_id="c1",
    )
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_tool_call_result_no_error() -> None:
    ev = ToolCallResult(request_id="r1", tool="t", result="ok", error=None, call_id="c1")
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_tool_call_result_with_error() -> None:
    ev = ToolCallResult(request_id="r1", tool="t", result="partial", error="x", call_id="c9")
    assert event_from_json(event_to_json(ev)) == ev


def test_sse_omits_tool_call_started_debug_fields() -> None:
    ev = ToolCallStarted(
        request_id="r1",
        tool="read_file",
        label="read_file",
        args={"path": "notes.md"},
        call_id="c1",
        inspector_decision="allow",
        resource="notes.md",
        resolved_path="notes.md",
    )
    payload = json.loads(event_to_json(ev))
    assert "inspector_decision" not in payload
    assert "resource" not in payload
    assert "resolved_path" not in payload


def test_sse_omits_tool_call_result_debug_fields() -> None:
    ev = ToolCallResult(
        request_id="r1",
        tool="read_file",
        result="ok",
        error=None,
        call_id="c1",
        error_kind="runtime",
        duration_ms=12,
    )
    payload = json.loads(event_to_json(ev))
    assert "error_kind" not in payload
    assert "ok" not in payload
    assert "duration_ms" not in payload


def test_sse_omits_system_context_updated_text() -> None:
    ev = SystemContextUpdated(
        request_id="r1", epoch_id=1, changed_sources=["current_request"], text="secret"
    )
    payload = json.loads(event_to_json(ev))
    assert "text" not in payload


def test_tool_call_started_without_call_id_defaults_empty() -> None:
    payload = '{"type":"ToolCallStarted","request_id":"r1","tool":"x","label":"L","args":{}}'
    out = event_from_json(payload)
    assert isinstance(out, ToolCallStarted)
    assert out.call_id == ""


def test_agent_event_roundtrip_turn_complete() -> None:
    ev = TurnComplete(request_id="r1", usage=UsageTotals(1, 2, 3, 0.5, 99, 42))
    assert event_from_json(event_to_json(ev)) == ev


def test_turn_complete_roundtrip_cache_fields() -> None:
    ev = TurnComplete(
        request_id="r1",
        usage=UsageTotals(
            cache_read_tokens=10,
            cache_creation_tokens=4,
            cached_tokens=14,
        ),
    )
    assert event_from_json(event_to_json(ev)) == ev


def test_usage_totals_from_legacy_dict_defaults_zero() -> None:
    payload = (
        '{"type":"TurnComplete","request_id":"r",'
        '"usage":{"input_tokens":1,"output_tokens":2,"cached_tokens":3}}'
    )
    out = event_from_json(payload)
    assert isinstance(out, TurnComplete)
    assert out.usage.cache_read_tokens == 0
    assert out.usage.cache_creation_tokens == 0


def test_event_to_json_includes_cache_keys() -> None:
    ev = TurnComplete(
        request_id="r1",
        usage=UsageTotals(cache_read_tokens=7, cache_creation_tokens=3),
    )
    parsed = json.loads(event_to_json(ev))
    usage = parsed["usage"]
    assert isinstance(usage, dict)
    assert "cache_read_tokens" in usage
    assert "cache_creation_tokens" in usage
    assert usage["cache_read_tokens"] == 7
    assert usage["cache_creation_tokens"] == 3


def test_agent_event_roundtrip_error() -> None:
    ev = Error(request_id="r1", error="boom")
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_context_summarizing() -> None:
    from monkeybot.core.runtime.events import ContextSummarizing

    ev = ContextSummarizing(
        request_id="r1", estimated_tokens=170_000, context_window_tokens=200_000
    )
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_context_summarized() -> None:
    from monkeybot.core.runtime.events import ContextSummarized

    ev = ContextSummarized(request_id="r1", turns_summarized=12)
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_context_usage() -> None:
    from monkeybot.core.runtime.events import ContextUsage

    ev = ContextUsage(request_id="r1", estimated_tokens=42_000, context_window_tokens=200_000)
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_system_prompt_snapshot() -> None:
    from monkeybot.core.runtime.events import SystemPromptSnapshot

    ev = SystemPromptSnapshot(request_id="r1", inner_turn=2, text="## Agent\n\nHello")
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_turn_complete_numeric_cost_usd_int() -> None:
    payload = '{"type":"TurnComplete","request_id":"r","usage":{"cost_usd":0}}'
    out = event_from_json(payload)
    assert isinstance(out, TurnComplete)
    assert out.usage.cost_usd == 0.0


def test_event_from_json_rejects_invalid_json() -> None:
    raw = "{not json"
    with pytest.raises(EventDecodeError, match="invalid JSON"):
        event_from_json(raw)


def test_event_from_json_rejects_missing_type() -> None:
    with pytest.raises(EventDecodeError, match="missing type"):
        event_from_json("{}")


def test_event_from_json_rejects_unknown_type() -> None:
    raw = '{"type":"Nope","request_id":"x"}'
    with pytest.raises(EventDecodeError, match="unknown"):
        event_from_json(raw)


def test_event_from_json_accepts_kind_alias() -> None:
    raw = json.dumps({"kind": "Thinking", "request_id": "rid"}, separators=(",", ":"))
    out = event_from_json(raw)
    assert out == Thinking(request_id="rid")


def test_tool_call_started_args_default_empty_object() -> None:
    payload = '{"type":"ToolCallStarted","request_id":"r1","tool":"x","label":"L"}'
    out = event_from_json(payload)
    assert isinstance(out, ToolCallStarted)
    assert out.args == {}


def test_grounding_event_roundtrip() -> None:
    ev = GroundingEvent(
        request_id="r1",
        sources=[{"title": "T", "uri": "https://a.com"}],
        search_queries=["q1", "q2"],
    )
    assert event_from_json(event_to_json(ev)) == ev


def test_grounding_event_roundtrip_empty() -> None:
    ev = GroundingEvent(request_id="r1")
    assert event_from_json(event_to_json(ev)) == ev


def test_credential_egress_blocked_roundtrip_with_origin() -> None:
    ev = CredentialEgressBlockedEvent(request_id="r1", scan_kind="canary", origin="https://a.com")
    assert event_from_json(event_to_json(ev)) == ev


def test_credential_egress_blocked_roundtrip_without_origin() -> None:
    ev = CredentialEgressBlockedEvent(request_id="r1", scan_kind="secret")
    raw = event_to_json(ev)
    assert "origin" not in json.loads(raw)
    assert event_from_json(raw) == ev


def test_sse_image_block_roundtrip() -> None:
    ev = ImageBlock(request_id="r", image_id="c1:0", mime_type="image/png", data="abc")
    assert event_from_json(event_to_json(ev)) == ev


def test_sse_image_block_path_omits_data_on_wire() -> None:
    ev = ImageBlock(
        request_id="r",
        image_id="c1:0",
        mime_type="image/png",
        path="./generated-media/images/x.png",
    )
    raw = event_to_json(ev)
    assert "data" not in raw or '"data"' not in raw
    import json

    d = json.loads(raw)
    assert d["path"] == "./generated-media/images/x.png"
    assert "data" not in d
    assert event_from_json(raw).path == "./generated-media/images/x.png"


def test_sse_image_block_roundtrip_without_image_id() -> None:
    ev = ImageBlock(request_id="r", mime_type="image/png", data="abc")
    assert event_from_json(event_to_json(ev)) == ev


@pytest.mark.parametrize("signature", (None, "sig"))
def test_sse_thinking_block_delta_roundtrip(signature: str | None) -> None:
    ev = ThinkingBlockDelta(request_id="r", text="t", signature=signature)
    assert event_from_json(event_to_json(ev)) == ev


def test_sse_thinking_block_complete_roundtrip() -> None:
    ev = ThinkingBlockComplete(request_id="r", signature="anthropic-signature-or-empty")
    assert event_from_json(event_to_json(ev)) == ev


def test_sse_redacted_thinking_block_roundtrip() -> None:
    ev = RedactedThinkingBlock(request_id="r", data="opaque")
    assert event_from_json(event_to_json(ev)) == ev


@pytest.mark.parametrize("prompt", (None, "please confirm"))
def test_sse_tool_confirmation_request_event_roundtrip(prompt: str | None) -> None:
    ev = ToolConfirmationRequestEvent(
        request_id="r",
        tool_call_id="tc",
        tool_name="run_command",
        arguments={"x": 1},
        prompt=prompt,
    )
    assert event_from_json(event_to_json(ev)) == ev


@pytest.mark.parametrize(
    "action_type",
    ("elicitation", "toolConfirmation", "elicitationResponse"),
)
def test_sse_action_required_event_roundtrip(action_type: str) -> None:
    at = cast(Literal["elicitation", "toolConfirmation", "elicitationResponse"], action_type)
    ev = ActionRequiredEvent(
        request_id="r",
        action_type=at,
        id="e1",
        payload={"k": "v"},
    )
    assert event_from_json(event_to_json(ev)) == ev


def test_sse_frontend_tool_request_event_roundtrip() -> None:
    ev = FrontendToolRequestEvent(
        request_id="r",
        tool_call_id="f1",
        name="echo",
        args={"q": "hi"},
    )
    assert event_from_json(event_to_json(ev)) == ev


@pytest.mark.parametrize("data", (None, {"x": 1}))
def test_sse_system_notification_event_roundtrip(data: dict[str, object] | None) -> None:
    ev = SystemNotificationEvent(
        request_id="r",
        notification_type="creditsExhausted",
        msg="out of credits",
        data=data,
    )
    assert event_from_json(event_to_json(ev)) == ev


def test_config_reloaded_roundtrip() -> None:
    from monkeybot.core.runtime.events import ConfigReloaded

    ev = ConfigReloaded(
        request_id="",
        revision=3,
        digest="abc",
        hot=["MODEL_NAME"],
        applied=["MODEL_PROVIDER"],
        restart_required=["DB_URL"],
    )
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_user_steered() -> None:
    ev = UserSteered(request_id="r1", text="nudge")
    assert event_from_json(event_to_json(ev)) == ev


@pytest.mark.parametrize("queue", ("steer", "follow_up"))
def test_agent_event_roundtrip_queued_input_accepted(queue: str) -> None:
    q = cast(Literal["steer", "follow_up"], queue)
    ev = QueuedInputAccepted(request_id="r1", queue=q, position=2)
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_context_epoch_started() -> None:
    from monkeybot.core.runtime.events import ContextEpochStarted

    ev = ContextEpochStarted(request_id="r1", epoch_id=2, changed_sources=["epoch"])
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_system_context_updated() -> None:
    from monkeybot.core.runtime.events import SystemContextUpdated

    ev = SystemContextUpdated(request_id="r1", epoch_id=1, changed_sources=["memory"])
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_assistant_text_bounds() -> None:
    from monkeybot.core.runtime.events import AssistantTextEnded, AssistantTextStarted

    started = AssistantTextStarted(request_id="r1")
    ended = AssistantTextEnded(request_id="r1")
    assert event_from_json(event_to_json(started)) == started
    assert event_from_json(event_to_json(ended)) == ended


def test_agent_event_roundtrip_thinking_block_started() -> None:
    from monkeybot.core.runtime.events import ThinkingBlockStarted

    ev = ThinkingBlockStarted(request_id="r1")
    assert event_from_json(event_to_json(ev)) == ev


def test_agent_event_roundtrip_tool_input_delta() -> None:
    from monkeybot.core.runtime.events import ToolInputDeltaEvent

    ev = ToolInputDeltaEvent(request_id="r1", call_id="c1", tool="read_file", delta='{"p')
    assert event_from_json(event_to_json(ev)) == ev


def test_subagent_started_is_agent_event() -> None:
    from monkeybot.core.runtime.events import SubagentStarted

    ev: AgentEvent = SubagentStarted(
        request_id="parent-req",
        parent_call_id="task-call-1",
        run_id="run-uuid",
        child_thread_id="subagent:sess:abc123def0",
        subagent_type="researcher",
        task="Investigate foo",
        label="Research",
    )
    assert ev.kind == "SubagentStarted"
    assert isinstance(ev, SubagentStarted)
    assert SubagentStarted in get_args(AgentEvent)


def test_subagent_event_is_agent_event() -> None:
    from monkeybot.core.runtime.events import SubagentEvent

    ev: AgentEvent = SubagentEvent(
        request_id="parent-req",
        parent_call_id="task-call-1",
        run_id="run-uuid",
        child_thread_id="subagent:sess:abc123def0",
        subagent_type="researcher",
        inner=AssistantDelta(request_id="child-req", delta="hi"),
    )
    assert ev.kind == "SubagentEvent"
    assert isinstance(ev, SubagentEvent)
    assert SubagentEvent in get_args(AgentEvent)


def test_subagent_completed_is_agent_event() -> None:
    from monkeybot.core.runtime.events import SubagentCompleted

    ev: AgentEvent = SubagentCompleted(
        request_id="parent-req",
        parent_call_id="task-call-1",
        run_id="run-uuid",
        child_thread_id="subagent:sess:abc123def0",
        subagent_type="researcher",
        ok=True,
        final_message="done",
        errors=[],
        tool_call_count=0,
    )
    assert ev.kind == "SubagentCompleted"
    assert isinstance(ev, SubagentCompleted)
    assert SubagentCompleted in get_args(AgentEvent)


def test_subagent_started_roundtrip() -> None:
    from monkeybot.core.runtime.events import SubagentStarted

    ev = SubagentStarted(
        request_id="parent-req",
        parent_call_id="task-call-1",
        run_id="run-uuid",
        child_thread_id="subagent:sess:abc123def0",
        subagent_type="researcher",
        task="Investigate foo",
        label="Research",
    )
    assert event_from_json(event_to_json(ev)) == ev


def test_subagent_started_roundtrip_null_subagent_type() -> None:
    from monkeybot.core.runtime.events import SubagentStarted

    ev = SubagentStarted(
        request_id="parent-req",
        parent_call_id="task-call-1",
        run_id="run-uuid",
        child_thread_id="subagent:sess:abc123def0",
        subagent_type=None,
        task="Investigate foo",
        label="Research",
    )
    out = event_from_json(event_to_json(ev))
    assert out == ev
    payload = json.loads(event_to_json(ev))
    assert payload["subagent_type"] is None


def test_subagent_event_roundtrip_preserves_inner() -> None:
    from monkeybot.core.runtime.events import SubagentEvent

    inner = ToolCallStarted(
        request_id="child-r",
        tool="read_file",
        label="Read",
        args={"path": "a.py"},
        call_id="c-child",
    )
    ev = SubagentEvent(
        request_id="parent-req",
        parent_call_id="task-call-1",
        run_id="run-uuid",
        child_thread_id="subagent:sess:abc123def0",
        subagent_type="researcher",
        inner=inner,
    )
    out = event_from_json(event_to_json(ev))
    assert out == ev
    assert isinstance(out, SubagentEvent)
    assert out.inner.request_id == "child-r"
    assert out.request_id == "parent-req"


def test_subagent_event_roundtrip_assistant_delta_inner() -> None:
    from monkeybot.core.runtime.events import SubagentEvent

    ev = SubagentEvent(
        request_id="parent-req",
        parent_call_id="task-call-1",
        run_id="run-uuid",
        child_thread_id="subagent:sess:abc123def0",
        subagent_type="researcher",
        inner=AssistantDelta(request_id="c", delta="café"),
    )
    assert event_from_json(event_to_json(ev)) == ev


def test_subagent_event_wire_uses_type_and_nested_inner() -> None:
    from monkeybot.core.runtime.events import SubagentEvent

    ev = SubagentEvent(
        request_id="parent-req",
        parent_call_id="task-call-1",
        run_id="run-uuid",
        child_thread_id="subagent:sess:abc123def0",
        subagent_type="researcher",
        inner=AssistantDelta(request_id="child-req", delta="hi"),
    )
    payload = json.loads(event_to_json(ev))
    assert payload["type"] == "SubagentEvent"
    assert isinstance(payload["inner"], dict)
    assert payload["inner"]["type"] == "AssistantDelta"


def test_subagent_completed_roundtrip() -> None:
    from monkeybot.core.runtime.events import SubagentCompleted

    ev = SubagentCompleted(
        request_id="parent-req",
        parent_call_id="task-call-1",
        run_id="run-uuid",
        child_thread_id="subagent:sess:abc123def0",
        subagent_type="researcher",
        ok=False,
        final_message="done",
        errors=["boom"],
        tool_call_count=2,
    )
    assert event_from_json(event_to_json(ev)) == ev


def test_subagent_event_decode_rejects_missing_inner() -> None:
    raw = json.dumps(
        {
            "type": "SubagentEvent",
            "request_id": "parent-req",
            "parent_call_id": "task-call-1",
            "run_id": "run-uuid",
            "child_thread_id": "subagent:sess:abc123def0",
            "subagent_type": "researcher",
        }
    )
    with pytest.raises(EventDecodeError, match="SubagentEvent inner must be an object"):
        event_from_json(raw)


def test_subagent_event_decode_rejects_non_object_inner() -> None:
    raw = json.dumps(
        {
            "type": "SubagentEvent",
            "request_id": "parent-req",
            "parent_call_id": "task-call-1",
            "run_id": "run-uuid",
            "child_thread_id": "subagent:sess:abc123def0",
            "subagent_type": "researcher",
            "inner": "nope",
        }
    )
    with pytest.raises(EventDecodeError, match="SubagentEvent inner must be an object"):
        event_from_json(raw)


def test_is_subagent_forwardable_allowlist() -> None:
    from monkeybot.core.runtime.events import is_subagent_forwardable

    allowlisted: list[AgentEvent] = [
        AssistantDelta(request_id="r", delta="x"),
        AssistantTextStarted(request_id="r"),
        AssistantTextEnded(request_id="r", text="done"),
        ThinkingBlockStarted(request_id="r"),
        ThinkingBlockDelta(request_id="r", text="t"),
        ThinkingBlockComplete(request_id="r", signature="sig"),
        RedactedThinkingBlock(request_id="r", data="opaque"),
        ToolCallStarted(request_id="r", tool="read_file", label="Read", call_id="c1"),
        ToolCallResult(request_id="r", tool="read_file", result="ok", call_id="c1"),
        ToolInputDeltaEvent(request_id="r", call_id="c1", tool="read_file", delta='{"p'),
        Error(request_id="r", error="boom"),
        TurnComplete(request_id="r"),
    ]
    assert all(is_subagent_forwardable(ev) for ev in allowlisted)


def test_is_subagent_forwardable_denylist() -> None:
    from monkeybot.core.runtime.events import SubagentStarted, is_subagent_forwardable

    denylisted: list[AgentEvent] = [
        SystemPromptSnapshot(request_id="r", inner_turn=1, text="## Agent"),
        ContextUsage(request_id="r", estimated_tokens=1, context_window_tokens=2),
        ToolConfirmationRequestEvent(
            request_id="r",
            tool_call_id="tc",
            tool_name="run_command",
            arguments={"x": 1},
        ),
        Thinking(request_id="r"),
        ImageBlock(request_id="r", mime_type="image/png", data="abc"),
        SubagentStarted(
            request_id="p",
            parent_call_id="c",
            run_id="r",
            child_thread_id="t",
        ),
    ]
    assert all(not is_subagent_forwardable(ev) for ev in denylisted)


def test_wrap_subagent_event_returns_wrapper() -> None:
    from monkeybot.core.runtime.events import SubagentEvent, wrap_subagent_event

    inner = ToolCallResult(
        request_id="child-req",
        tool="read_file",
        result="ok",
        call_id="c-child",
    )
    wrapped = wrap_subagent_event(
        request_id="parent-req",
        parent_call_id="task-call-1",
        run_id="run-uuid",
        child_thread_id="subagent:sess:abc123def0",
        subagent_type="researcher",
        inner=inner,
    )
    assert wrapped is not None
    assert isinstance(wrapped, SubagentEvent)
    assert wrapped.inner == inner
    assert wrapped.request_id == "parent-req"
    assert wrapped.parent_call_id == "task-call-1"
    assert wrapped.run_id == "run-uuid"
    assert wrapped.child_thread_id == "subagent:sess:abc123def0"
    assert wrapped.subagent_type == "researcher"


def test_wrap_subagent_event_returns_none_for_denylisted() -> None:
    from monkeybot.core.runtime.events import wrap_subagent_event

    inner = SystemPromptSnapshot(request_id="r", inner_turn=1, text="## Agent")
    wrapped = wrap_subagent_event(
        request_id="parent-req",
        parent_call_id="task-call-1",
        run_id="run-uuid",
        child_thread_id="subagent:sess:abc123def0",
        subagent_type="researcher",
        inner=inner,
    )
    assert wrapped is None
