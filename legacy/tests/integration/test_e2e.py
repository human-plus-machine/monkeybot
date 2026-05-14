"""End-to-end integration tests.

Exercises the full cross-story path:
  AgentLoop (Story 6) → ConversationHistory (Story 2) → tools (Story 3) →
  core/memory (Story 2) + core/context (Story 1) + RulesInspector (Story 1)

FakeProvider drives the LLM side so no API key is needed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.events import (
    AssistantDelta,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
    UserMessage,
)
from monkeybot.core.history import ConversationHistory
from monkeybot.core.inspector import RulesInspector
from monkeybot.core.loop import AgentLoop
from monkeybot.core.provider import (
    Message,
    Provider,
    ProviderDone,
    ProviderUsage,
    TextDelta,
    ToolCall,
    ToolDef,
)

# ---------------------------------------------------------------------------
# FakeProvider (same pattern as test_loop.py)
# ---------------------------------------------------------------------------

class FakeProvider:
    name = "fake"
    supports_streaming = True

    def __init__(self, event_batches: list[list]) -> None:
        self._batches = iter(event_batches)

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str,
        system: str,
        context: object = None,
    ) -> object:
        batch = next(self._batches)

        async def _gen() -> object:
            for event in batch:
                yield event

        return _gen()


assert isinstance(FakeProvider([[]]), Provider)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture
async def env(tmp_path: Path) -> tuple[Path, ConversationHistory]:
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# TestBot\nYou are a test bot.")
    (tmp_path / "memory").mkdir()
    (tmp_path / "skills").mkdir()
    history = ConversationHistory(db_url=f"sqlite:///{tmp_path}/test.db")
    await history.init()
    return tmp_path, history


def _make_loop(
    tmp_path: Path,
    history: ConversationHistory,
    provider: Provider,
    inspectors: list | None = None,
) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        history=history,
        inspectors=inspectors or [],
        config={
            "agent_md_path": str(tmp_path / "AGENT.md"),
            "memory_path": str(tmp_path / "memory"),
            "skills_path": str(tmp_path / "skills"),
            "bot_dir": str(tmp_path),
            "model": "fake",
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_full_text_turn_event_order(env: tuple) -> None:
    """UserMessage → AssistantDelta* → TurnComplete in correct order."""
    tmp_path, history = env
    provider = FakeProvider([[
        TextDelta(text="Hi "),
        TextDelta(text="there!"),
        ProviderDone(usage=ProviderUsage(input_tokens=8, output_tokens=4)),
    ]])
    loop = _make_loop(tmp_path, history, provider)
    events = [e async for e in loop.run("Hello", "sess-order")]

    assert isinstance(events[0], UserMessage)
    assert isinstance(events[-1], TurnComplete)
    deltas = [e for e in events if isinstance(e, AssistantDelta)]
    assert "".join(e.text for e in deltas) == "Hi there!"


async def test_memory_tool_reads_core_memory(env: tuple) -> None:
    """search_memory tool delegates to core/memory.py and returns real file content."""
    tmp_path, history = env
    (tmp_path / "memory" / "facts.md").write_text("The capital of France is Paris.")

    provider = FakeProvider([
        [
            ToolCall(call_id="c1", name="search_memory", args={"query": "France"}),
            ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=1)),
        ],
        [
            TextDelta(text="Paris."),
            ProviderDone(usage=ProviderUsage(input_tokens=10, output_tokens=2)),
        ],
    ])
    loop = _make_loop(tmp_path, history, provider)
    events = [e async for e in loop.run("What is the capital of France?", "sess-mem")]

    tool_results = [e for e in events if isinstance(e, ToolCallResult)]
    assert len(tool_results) == 1
    assert "Paris" in (tool_results[0].result or "")


async def test_write_then_search_memory(env: tuple) -> None:
    """write_file saves a memory file; search_memory finds it in the same turn."""
    tmp_path, history = env
    mem_file = str(tmp_path / "memory" / "capital.md")

    provider = FakeProvider([
        [
            ToolCall(
                call_id="c1",
                name="write_file",
                args={"path": mem_file, "content": "Rome is the capital of Italy."},
            ),
            ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=1)),
        ],
        [
            ToolCall(call_id="c2", name="search_memory", args={"query": "Italy"}),
            ProviderDone(usage=ProviderUsage(input_tokens=6, output_tokens=1)),
        ],
        [
            TextDelta(text="Rome."),
            ProviderDone(usage=ProviderUsage(input_tokens=10, output_tokens=2)),
        ],
    ])
    loop = _make_loop(tmp_path, history, provider)
    events = [e async for e in loop.run("Tell me about Italy", "sess-write-search")]

    tool_results = [e for e in events if isinstance(e, ToolCallResult)]
    assert len(tool_results) == 2
    search_result = tool_results[1]
    assert "Rome" in (search_result.result or "")


async def test_inspector_denies_tool_call(env: tuple) -> None:
    """RulesInspector blocks run_command with 'sudo'; ToolCallResult has error."""
    tmp_path, history = env
    provider = FakeProvider([
        [
            ToolCall(call_id="c1", name="run_command", args={"command": "sudo rm -rf /"}),
            ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=1)),
        ],
        [
            TextDelta(text="Blocked."),
            ProviderDone(usage=ProviderUsage(input_tokens=8, output_tokens=2)),
        ],
    ])
    inspector = RulesInspector(denied_patterns=["sudo"])
    loop = _make_loop(tmp_path, history, provider, inspectors=[inspector])
    events = [e async for e in loop.run("Run sudo", "sess-denied")]

    tool_results = [e for e in events if isinstance(e, ToolCallResult)]
    assert len(tool_results) == 1
    assert tool_results[0].error is not None
    assert "Denied" in tool_results[0].error
    # ToolCallStarted must NOT have been emitted for a denied call
    assert not any(isinstance(e, ToolCallStarted) for e in events)


async def test_multi_turn_history_accumulates(env: tuple) -> None:
    """Second turn includes messages from the first turn in history."""
    tmp_path, history = env

    provider1 = FakeProvider([[
        TextDelta(text="I am MonkeyBot."),
        ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=5)),
    ]])
    loop1 = _make_loop(tmp_path, history, provider1)
    [_ async for _ in loop1.run("Who are you?", "sess-multi")]

    # After turn 1 history has user + assistant messages
    msgs_after_1 = await history.load("sess-multi")
    assert len(msgs_after_1) == 2
    assert msgs_after_1[0].role == "user"
    assert msgs_after_1[1].role == "assistant"

    provider2 = FakeProvider([[
        TextDelta(text="Yes."),
        ProviderDone(usage=ProviderUsage(input_tokens=10, output_tokens=2)),
    ]])
    loop2 = _make_loop(tmp_path, history, provider2)
    [_ async for _ in loop2.run("Do you remember?", "sess-multi")]

    msgs_after_2 = await history.load("sess-multi")
    assert len(msgs_after_2) == 4  # 2 original + 2 new


async def test_public_api_imports_resolve() -> None:
    """All four public exports from monkeybot.__init__ resolve lazily without error."""
    import monkeybot

    assert monkeybot.AgentLoop is AgentLoop
    from monkeybot.core.history import ConversationHistory
    assert monkeybot.ConversationHistory is ConversationHistory
    from monkeybot.core.provider import Provider
    assert monkeybot.Provider is Provider
    from monkeybot.core.context import TurnContext
    assert monkeybot.TurnContext is TurnContext
