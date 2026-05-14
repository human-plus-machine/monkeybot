"""Unit tests for AgentLoop using FakeProvider."""
from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.events import (
    AssistantDelta,
    ErrorEvent,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
    UserMessage,
)
from monkeybot.core.history import ConversationHistory
from monkeybot.core.loop import AgentLoop
from monkeybot.core.provider import ProviderDone, ProviderUsage, TextDelta, ToolCall


class FakeProvider:
    name = "fake"
    supports_streaming = True

    def __init__(self, event_batches: list[list]) -> None:  # type: ignore[type-arg]
        self._batches = iter(event_batches)

    async def stream(self, messages, tools, *, model, system, context=None):  # type: ignore[override]
        batch = next(self._batches)

        async def _gen():  # type: ignore[return]
            for event in batch:
                yield event

        return _gen()


@pytest.fixture
async def loop_env(tmp_path: Path):  # type: ignore[return]
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# TestBot\nYou are a test bot.")
    (tmp_path / "memory").mkdir()
    (tmp_path / "skills").mkdir()
    db_path = str(tmp_path / "test.db")
    history = ConversationHistory(db_url=f"sqlite:///{db_path}")
    await history.init()
    return tmp_path, history


async def make_loop(tmp_path: Path, history: ConversationHistory, provider: object) -> AgentLoop:
    return AgentLoop(
        provider=provider,  # type: ignore[arg-type]
        history=history,
        inspectors=[],
        config={
            "agent_md_path": str(tmp_path / "AGENT.md"),
            "memory_path": str(tmp_path / "memory"),
            "skills_path": str(tmp_path / "skills"),
            "bot_dir": str(tmp_path),
            "model": "fake",
        },
    )


async def test_simple_text_response(loop_env) -> None:  # type: ignore[return]
    tmp_path, history = loop_env
    provider = FakeProvider(
        [
            [
                TextDelta(text="Hello, "),
                TextDelta(text="world!"),
                ProviderDone(usage=ProviderUsage(input_tokens=10, output_tokens=5)),
            ]
        ]
    )
    loop = await make_loop(tmp_path, history, provider)

    events = [e async for e in loop.run("Hi", "sess-1")]

    assert isinstance(events[0], UserMessage)
    text_events = [e for e in events if isinstance(e, AssistantDelta)]
    assert "".join(e.text for e in text_events) == "Hello, world!"
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].input_tokens == 10
    assert events[-1].output_tokens == 5


async def test_turn_complete_always_last_on_error(loop_env) -> None:  # type: ignore[return]
    """Even if provider raises, TurnComplete must be the final event."""
    tmp_path, history = loop_env

    class BrokenProvider:
        name = "broken"
        supports_streaming = True

        async def stream(self, *a, **kw):  # type: ignore[override]
            async def _gen():  # type: ignore[return]
                raise RuntimeError("provider exploded")
                yield  # unreachable — marks this as an async generator

            return _gen()

    loop = await make_loop(tmp_path, history, BrokenProvider())
    events = [e async for e in loop.run("Hi", "sess-err")]
    assert isinstance(events[-1], TurnComplete)
    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(error_events) >= 1


async def test_tool_call_cycle(loop_env) -> None:  # type: ignore[return]
    """Loop re-enters provider after tool call."""
    tmp_path, history = loop_env
    provider = FakeProvider(
        [
            [
                ToolCall(
                    call_id="c1",
                    name="read_file",
                    args={"path": str(tmp_path / "AGENT.md")},
                ),
                ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=1)),
            ],
            [
                TextDelta(text="Done."),
                ProviderDone(usage=ProviderUsage(input_tokens=8, output_tokens=3)),
            ],
        ]
    )
    loop = await make_loop(tmp_path, history, provider)
    events = [e async for e in loop.run("Read the agent file", "sess-tool")]

    assert any(isinstance(e, ToolCallStarted) for e in events)
    assert any(isinstance(e, ToolCallResult) for e in events)
    assert any(isinstance(e, AssistantDelta) and e.text == "Done." for e in events)
    assert isinstance(events[-1], TurnComplete)


async def test_unknown_tool_returns_error_string(loop_env) -> None:  # type: ignore[return]
    tmp_path, history = loop_env
    provider = FakeProvider(
        [
            [
                ToolCall(call_id="c1", name="nonexistent_tool", args={}),
                ProviderDone(usage=ProviderUsage(input_tokens=3, output_tokens=1)),
            ],
            [
                TextDelta(text="OK"),
                ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=1)),
            ],
        ]
    )
    loop = await make_loop(tmp_path, history, provider)
    events = [e async for e in loop.run("Use nonexistent tool", "sess-unknown")]
    tool_results = [e for e in events if isinstance(e, ToolCallResult)]
    assert any("Unknown tool" in (e.result or "") for e in tool_results)


async def test_history_persists_after_turn(loop_env) -> None:  # type: ignore[return]
    tmp_path, history = loop_env
    provider = FakeProvider(
        [
            [
                TextDelta(text="Response."),
                ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=3)),
            ]
        ]
    )
    loop = await make_loop(tmp_path, history, provider)
    [_ async for _ in loop.run("Hello", "sess-persist")]
    msgs = await history.load("sess-persist")
    assert any(m.role == "user" for m in msgs)
    assert any(m.role == "assistant" for m in msgs)
