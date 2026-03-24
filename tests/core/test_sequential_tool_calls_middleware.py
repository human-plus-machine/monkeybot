"""SequentialToolCallsMiddleware keeps one tool call per model turn."""

from __future__ import annotations

from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage

from emonk.core.agent_guard_middleware import SequentialToolCallsMiddleware


def test_trims_to_first_tool_call_when_multiple() -> None:
    mw = SequentialToolCallsMiddleware()
    ai = AIMessage(
        content="",
        tool_calls=[
            {"name": "task", "args": {"x": 1}, "id": "call_1", "type": "tool_call"},
            {"name": "task", "args": {"y": 2}, "id": "call_2", "type": "tool_call"},
        ],
    )
    mr = ModelResponse(result=[ai])
    out = mw._trim_model_response(mr)
    assert len(out.result) == 1
    assert isinstance(out.result[0], AIMessage)
    assert len(out.result[0].tool_calls) == 1
    assert out.result[0].tool_calls[0]["id"] == "call_1"


def test_noop_when_single_tool_call() -> None:
    mw = SequentialToolCallsMiddleware()
    ai = AIMessage(
        content="",
        tool_calls=[
            {"name": "execute", "args": {"command": "echo hi"}, "id": "c1", "type": "tool_call"},
        ],
    )
    mr = ModelResponse(result=[ai])
    out = mw._trim_model_response(mr)
    assert out is mr


def test_respects_env_disable(monkeypatch) -> None:
    monkeypatch.setenv("EMONK_SEQUENTIAL_TOOL_CALLS", "0")
    mw = SequentialToolCallsMiddleware()
    ai = AIMessage(
        content="",
        tool_calls=[
            {"name": "a", "args": {}, "id": "1", "type": "tool_call"},
            {"name": "b", "args": {}, "id": "2", "type": "tool_call"},
        ],
    )
    mr = ModelResponse(result=[ai])
    out = mw._maybe_trim(mr)
    assert out is mr
