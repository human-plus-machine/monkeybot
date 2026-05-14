"""E4 integration tests — subagent pipeline wired through AgentLoop.

Covers:
  - spawn_subagent tool dispatch via AgentLoop (happy path)
  - DurableRunStore lifecycle tracking across spawn
  - SubagentRegistry prompt block injection into system prompt
  - Error path: unknown registry name returns ToolCallResult with error
"""
from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.durable_runs import DurableRunStore
from monkeybot.core.events import (
    SubagentCompleted,
    SubagentStarted,
    ToolCallResult,
)
from monkeybot.core.history import ConversationHistory
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
from monkeybot.core.subagent_registry import SubagentRegistry

ECHO_AGENT = str(Path(__file__).parent.parent / "fixtures" / "echo_agent.py")


# ---------------------------------------------------------------------------
# FakeProvider
# ---------------------------------------------------------------------------

class FakeProvider:
    name = "fake"
    supports_streaming = True

    def __init__(self, event_batches: list[list]) -> None:
        self._batches = iter(event_batches)
        self.last_system: str = ""

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str,
        system: str,
        context: object = None,
    ) -> object:
        self.last_system = system
        batch = next(self._batches)

        async def _gen() -> object:
            for event in batch:
                yield event

        return _gen()


assert isinstance(FakeProvider([[]]), Provider)


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

@pytest.fixture
async def env(tmp_path: Path) -> tuple[Path, ConversationHistory, DurableRunStore]:
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# TestBot\nYou are a test bot.")
    (tmp_path / "memory").mkdir()
    (tmp_path / "skills").mkdir()

    history = ConversationHistory(db_url=f"sqlite:///{tmp_path}/test.db")
    await history.init()

    store = DurableRunStore(db_path=str(tmp_path / "runs.db"))
    await store.init()

    return tmp_path, history, store


def _make_loop(
    tmp_path: Path,
    history: ConversationHistory,
    provider: FakeProvider,
    store: DurableRunStore | None = None,
    registry: SubagentRegistry | None = None,
) -> AgentLoop:
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
        registry=registry,
        durable_store=store,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_spawn_subagent_via_script_happy_path(
    env: tuple[Path, ConversationHistory, DurableRunStore],
) -> None:
    """AgentLoop dispatches spawn_subagent → SubagentStarted + TurnComplete + SubagentCompleted."""
    tmp_path, history, store = env
    provider = FakeProvider([
        [
            ToolCall(
                call_id="c1",
                name="spawn_subagent",
                args={"script": ECHO_AGENT, "task": "echo test"},
            ),
            ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=1)),
        ],
        [
            TextDelta(text="Done."),
            ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=2)),
        ],
    ])
    loop = _make_loop(tmp_path, history, provider, store=store)
    events = [e async for e in loop.run("spawn", "sess-spawn")]

    kinds = [type(e).__name__ for e in events]
    assert "ToolCallStarted" in kinds
    assert "SubagentStarted" in kinds
    assert "TurnComplete" in kinds  # from echo_agent
    assert "SubagentCompleted" in kinds
    assert "ToolCallResult" in kinds

    tool_result = next(e for e in events if isinstance(e, ToolCallResult))
    assert "run_id=" in (tool_result.result or "")
    assert tool_result.error is None


async def test_spawn_subagent_records_in_durable_store(
    env: tuple[Path, ConversationHistory, DurableRunStore],
) -> None:
    """DurableRunStore has no pending runs after a successful spawn."""
    tmp_path, history, store = env
    provider = FakeProvider([
        [
            ToolCall(
                call_id="c1",
                name="spawn_subagent",
                args={"script": ECHO_AGENT, "task": "record test"},
            ),
            ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=1)),
        ],
        [
            TextDelta(text="ok"),
            ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=1)),
        ],
    ])
    loop = _make_loop(tmp_path, history, provider, store=store)
    [_ async for _ in loop.run("spawn", "sess-durable")]

    pending = await store.pending_runs()
    assert pending == [], "Completed run should not be in pending list"


async def test_registry_prompt_block_injected_into_system_prompt(
    env: tuple[Path, ConversationHistory, DurableRunStore],
) -> None:
    """SubagentRegistry.to_prompt_block() text appears in the system prompt sent to provider."""
    tmp_path, history, store = env
    registry = SubagentRegistry(
        {"researcher": {"script": ECHO_AGENT, "description": "Does research"}},
        bot_skills_path=str(tmp_path / "skills"),
        bot_model="fake",
    )
    provider = FakeProvider([
        [
            TextDelta(text="hi"),
            ProviderDone(usage=ProviderUsage(input_tokens=2, output_tokens=1)),
        ],
    ])
    loop = _make_loop(tmp_path, history, provider, store=store, registry=registry)
    [_ async for _ in loop.run("hello", "sess-registry")]

    assert "Available Subagents" in provider.last_system
    assert "researcher" in provider.last_system
    assert "Does research" in provider.last_system


async def test_spawn_unknown_registry_name_returns_error(
    env: tuple[Path, ConversationHistory, DurableRunStore],
) -> None:
    """spawn_subagent with unknown 'name' yields ToolCallResult with error, no crash."""
    tmp_path, history, store = env
    registry = SubagentRegistry(
        {"researcher": {"script": ECHO_AGENT, "description": "Does research"}},
        bot_skills_path=str(tmp_path / "skills"),
        bot_model="fake",
    )
    provider = FakeProvider([
        [
            ToolCall(
                call_id="c2",
                name="spawn_subagent",
                args={"name": "nonexistent", "task": "fail"},
            ),
            ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=1)),
        ],
        [
            TextDelta(text="error handled"),
            ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=2)),
        ],
    ])
    loop = _make_loop(tmp_path, history, provider, store=store, registry=registry)
    events = [e async for e in loop.run("bad spawn", "sess-unknown")]

    tool_results = [e for e in events if isinstance(e, ToolCallResult)]
    assert len(tool_results) == 1
    assert "ERROR" in (tool_results[0].result or "")
    assert "nonexistent" in (tool_results[0].result or "")


async def test_spawn_via_registry_name_resolves_and_runs(
    env: tuple[Path, ConversationHistory, DurableRunStore],
) -> None:
    """spawn_subagent with valid registry 'name' resolves and executes the subagent."""
    tmp_path, history, store = env
    registry = SubagentRegistry(
        {"echo": {"script": ECHO_AGENT, "description": "Echo agent"}},
        bot_skills_path=str(tmp_path / "skills"),
        bot_model="fake",
    )
    provider = FakeProvider([
        [
            ToolCall(
                call_id="c3",
                name="spawn_subagent",
                args={"name": "echo", "task": "say hi"},
            ),
            ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=1)),
        ],
        [
            TextDelta(text="done"),
            ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=1)),
        ],
    ])
    loop = _make_loop(tmp_path, history, provider, store=store, registry=registry)
    events = [e async for e in loop.run("run echo", "sess-registry-run")]

    assert any(isinstance(e, SubagentStarted) for e in events)
    assert any(isinstance(e, SubagentCompleted) for e in events)

    tool_result = next(e for e in events if isinstance(e, ToolCallResult))
    assert tool_result.error is None
