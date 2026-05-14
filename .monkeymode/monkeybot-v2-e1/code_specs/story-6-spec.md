# Code Spec: Story 6 — Agent Loop, Gateway & CLI

**Story:** user_stories.md "Story 6: Agent Loop, Gateway & CLI"  
**Design Reference:** 1b-contracts.md "core/loop.py", "gateway/cli.py", "cli.py", "Unit Testing — FakeProvider", 1c-operations.md "Structured Logging", "Async Patterns"  
**Date:** 2026-05-13  
**Complexity:** M  
**Batch:** 2 (requires all Batch 1 stories merged)

## Implementation Summary
- **Files to Create:** 5 (3 source + 2 test)
- **Files to Modify:** 0
- **Estimated LOC:** ~300 source, ~200 test

## Codebase Conventions

All prior conventions apply. Additional notes for this story:
- Loop class manages lifecycle — `__init__` receives deps, `run()` executes a single turn
- `async for event in loop.run(...)` — callers use `AsyncIterator[AgentEvent]`
- All tool functions except `run_command` are sync; wrap with `asyncio.to_thread`
- `run_id` is a fresh ULID at the top of each `run()` call (not per-instance)
- Structured logging: `logging.getLogger("monkeybot.loop")` with a dict `extra=` for context

## Implementation Order

1. `core/loop.py` (the core logic)
2. `gateway/cli.py` (thin wrapper over the loop)
3. `src/monkeybot/cli.py` (click entry point wiring everything together)
4. `tests/unit/test_loop.py` (FakeProvider-based unit tests)
5. `tests/test_cold_start.py` (subprocess-based timing tests)

---

## Task 1: `core/loop.py`

**Files:** `src/monkeybot/core/loop.py` (create)  
**Deps:** All Story 1–3 modules, `asyncio`, `time`, `logging`, `ulid`

**Full class skeleton:**

```python
from __future__ import annotations
import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
import ulid

from monkeybot.core.context import load_turn_context
from monkeybot.core.events import (
    AgentEvent, UserMessage, AssistantDelta, ToolCallStarted, ToolCallResult,
    TurnComplete, ErrorEvent,
)
from monkeybot.core.history import ConversationHistory
from monkeybot.core.inspector import Decision, ToolInspector
from monkeybot.core.provider import Message, Provider, ProviderDone, TextDelta, ToolCall

log = logging.getLogger("monkeybot.loop")


class AgentLoop:
    def __init__(
        self,
        provider: Provider,
        history: ConversationHistory,
        inspectors: list[ToolInspector],
        config: dict[str, Any],
    ) -> None:
        self._provider = provider
        self._history = history
        self._inspectors = inspectors
        self._config = config
        self._tools = self._build_tool_registry()

    def _build_tool_registry(self) -> dict[str, Any]:
        """Hard-coded 5-tool registry. Returns {name: callable}."""
        ...

    async def run(
        self,
        user_message: str,
        session_id: str,
        user_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        ...
```

**`_build_tool_registry()` implementation:**
```python
from monkeybot.tools.run_command import run_command, format_result
from monkeybot.tools.file_ops import read_file, write_file
from monkeybot.tools.memory_ops import search_memory
from monkeybot.tools.skill_ops import list_skills

memory_path = self._config.get("memory_path", "./data/memory")
skills_path = self._config.get("skills_path", "./.agents/skills")
bot_dir = self._config.get("bot_dir", ".")
allowed_roots = [Path(memory_path), Path(bot_dir)]

return {
    "run_command": lambda args: run_command(**args),    # already async
    "read_file":   lambda args: read_file(**{**args, "allowed_roots": allowed_roots}),
    "write_file":  lambda args: write_file(**{**args, "allowed_roots": allowed_roots}),
    "search_memory": lambda args: search_memory(**{**args, "memory_path": memory_path}),
    "list_skills": lambda args: list_skills(**{**args, "skills_path": skills_path}),
}
```

Note: sync lambdas are wrapped in `asyncio.to_thread` during dispatch; `run_command` is already async and called directly.

**`run()` algorithm:**

```python
async def run(self, user_message, session_id, user_id=None):
    run_id = str(ulid.new())
    start_ms = int(time.monotonic() * 1000)
    input_tokens = output_tokens = 0

    yield UserMessage(content=user_message, user_id=user_id)

    try:
        history_msgs = await self._history.load(session_id)
        await self._history.save(session_id, "user", user_message)

        ctx = load_turn_context(
            agent_md_path=self._config["agent_md_path"],
            memory_path=self._config.get("memory_path", "./data/memory"),
            skills_path=self._config.get("skills_path", "./.agents/skills"),
            user_id=user_id,
            run_id=run_id,
        )

        messages = list(history_msgs) + [Message(role="user", content=user_message)]
        tool_defs = self._get_tool_defs()
        full_response_parts: list[str] = []

        # Agentic loop: re-enter provider after each tool call
        while True:
            tool_called_this_iteration = False

            async for pev in await self._provider.stream(
                messages, tool_defs, model=self._config.get("model", "gemini-2.0-flash"),
                system=ctx.build_system_prompt(), context=ctx,
            ):
                if isinstance(pev, TextDelta):
                    yield AssistantDelta(text=pev.text)
                    full_response_parts.append(pev.text)

                elif isinstance(pev, ToolCall):
                    tool_called_this_iteration = True
                    result_str = await self._dispatch_tool(pev, ctx)
                    messages.append(Message(role="assistant", content="", tool_call_id=pev.call_id))
                    messages.append(Message(role="tool", content=result_str,
                                            tool_call_id=pev.call_id, tool_name=pev.name))

                elif isinstance(pev, ProviderDone):
                    input_tokens += pev.usage.input_tokens
                    output_tokens += pev.usage.output_tokens

            if not tool_called_this_iteration:
                break  # Model is done — no more tool calls

        full_response = "".join(full_response_parts)
        if full_response:
            await self._history.save(session_id, "assistant", full_response)

    except Exception as exc:
        yield ErrorEvent(message=str(exc), recoverable=True)
        log.error("Loop error in run_id=%s: %s", run_id, exc, exc_info=True)

    finally:
        duration_ms = int(time.monotonic() * 1000) - start_ms
        yield TurnComplete(
            run_id=run_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
        )
        log.info("turn_complete", extra={
            "session_id": session_id, "run_id": run_id,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "duration_ms": duration_ms,
        })
```

**`_dispatch_tool()` algorithm:**
```python
async def _dispatch_tool(self, call: ToolCall, ctx) -> str:
    # 1. Run inspector chain
    for inspector in self._inspectors:
        decision = await inspector.check(call, ctx)
        if decision.kind == "deny":
            yield ToolCallResult(call_id=call.call_id, tool_name=call.name,
                                 result="", error=f"Denied: {decision.message}")
            return f"ERROR: Tool denied: {decision.message}"
        # "approve" (HITL) not wired in E1 — treat as allow

    yield ToolCallStarted(call_id=call.call_id, tool_name=call.name, args=call.args)

    callable_ = self._tools.get(call.name)
    if callable_ is None:
        result = f"Unknown tool: {call.name}"
        yield ToolCallResult(call_id=call.call_id, tool_name=call.name, result=result)
        return result

    start = time.monotonic()
    try:
        if call.name == "run_command":
            from monkeybot.tools.run_command import format_result
            cmd_result = await callable_(call.args)
            result = format_result(cmd_result)
        else:
            result = await asyncio.to_thread(callable_, call.args)
    except Exception as exc:
        result = f"ERROR: {exc}"

    duration_ms = int((time.monotonic() - start) * 1000)
    yield ToolCallResult(call_id=call.call_id, tool_name=call.name,
                         result=result, duration_ms=duration_ms)
    return result
```

**Note on `_dispatch_tool`:** This method yields events AND returns a string. Make it an `async def` that yields into the outer generator by using a helper. Cleanest approach: separate `_run_inspectors` and `_call_tool`, then yield events in `run()` from those helpers. Alternatively, use a list to collect events and yield them inline. Choose the simplest approach — correctness over elegance.

**`_get_tool_defs()` — collects ToolDef objects from TOOL_DEF dicts:**
```python
from monkeybot.core.provider import ToolDef
from monkeybot.tools import run_command as rc_mod, file_ops, memory_ops, skill_ops

def _get_tool_defs(self) -> list[ToolDef]:
    defs = []
    for tool_def_dict in [rc_mod.TOOL_DEF, *file_ops.TOOL_DEFS, memory_ops.TOOL_DEF, skill_ops.TOOL_DEF]:
        defs.append(ToolDef(
            name=tool_def_dict["name"],
            description=tool_def_dict["description"],
            parameters=tool_def_dict["parameters"],
        ))
    return defs
```

---

## Task 2: `gateway/cli.py`

**Files:** `src/monkeybot/gateway/cli.py` (create)  
**Deps:** `loop.py`, `events.py`, `asyncio`, `sys`

```python
from __future__ import annotations
import asyncio
import sys
from monkeybot.core.events import (
    AssistantDelta, ToolCallStarted, ToolCallResult, TurnComplete, ErrorEvent,
)
from monkeybot.core.loop import AgentLoop


class CLIGateway:
    def __init__(self, loop: AgentLoop, session_id: str) -> None:
        self._loop = loop
        self._session_id = session_id

    async def run_interactive(self) -> None:
        print("MonkeyBot ready. Type 'exit' to quit.\n")
        while True:
            try:
                user_input = await asyncio.to_thread(input, "> ")
            except EOFError:
                break
            if user_input.strip().lower() == "exit":
                break
            if not user_input.strip():
                continue

            async for event in self._loop.run(user_input, self._session_id):
                if isinstance(event, AssistantDelta):
                    print(event.text, end="", flush=True)
                elif isinstance(event, ToolCallStarted):
                    args_preview = str(event.args)[:80]
                    print(f"\n[Tool: {event.tool_name}({args_preview})]")
                elif isinstance(event, ToolCallResult):
                    preview = (event.result or event.error or "")[:100]
                    print(f"[Result: {preview}]")
                elif isinstance(event, TurnComplete):
                    print(f"\n[{event.input_tokens}in/{event.output_tokens}out tokens, {event.duration_ms}ms]")
                elif isinstance(event, ErrorEvent):
                    print(f"\nError: {event.message}", file=sys.stderr)
```

---

## Task 3: `src/monkeybot/cli.py`

**Files:** `src/monkeybot/cli.py` (create)  
**Deps:** `click`, `asyncio`, `os`, `logging`, `ulid`

```python
from __future__ import annotations
import asyncio
import logging
import os
import sys
from pathlib import Path
import click
import ulid

from monkeybot.core.history import ConversationHistory
from monkeybot.core.loop import AgentLoop
from monkeybot.gateway.cli import CLIGateway


@click.group()
def main() -> None:
    """MonkeyBot v2 — lightweight agent framework."""


@main.command()
@click.option("--bot-dir", required=True, type=click.Path(exists=True), help="Bot directory containing AGENT.md")
@click.option("--session-id", default=None, help="Session ID (auto-generated if omitted)")
@click.option("--model", default=None, help="Override model (default from config.yaml or gemini-2.0-flash)")
def run(bot_dir: str, session_id: str | None, model: str | None) -> None:
    """Start an interactive agent session."""
    _setup_logging()
    asyncio.run(_run_async(bot_dir, session_id, model))


def _setup_logging() -> None:
    import json, time

    class _JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            log: dict = {
                "timestamp": self.formatTime(record),
                "level": record.levelname,
                "service": "monkeybot",
                "message": record.getMessage(),
            }
            if hasattr(record, "session_id"):
                log["session_id"] = record.session_id  # type: ignore[attr-defined]
            if hasattr(record, "run_id"):
                log["run_id"] = record.run_id  # type: ignore[attr-defined]
            for field in ("input_tokens", "output_tokens", "duration_ms"):
                if hasattr(record, field):
                    log[field] = getattr(record, field)
            return json.dumps(log)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    logging.basicConfig(
        handlers=[handler],
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    )


async def _run_async(bot_dir: str, session_id: str | None, model: str | None) -> None:
    bot_path = Path(bot_dir)
    agent_md_path = str(bot_path / "AGENT.md")
    config_path = bot_path / "config.yaml"

    # Load optional config.yaml
    bot_config: dict = {}
    if config_path.exists():
        import yaml
        bot_config = yaml.safe_load(config_path.read_text()) or {}

    # Build config dict
    config = {
        "agent_md_path": agent_md_path,
        "memory_path": os.getenv("MEMORY_PATH", "./data/memory"),
        "skills_path": os.getenv("SKILLS_PATH", "./.agents/skills"),
        "bot_dir": str(bot_path),
        "model": model or bot_config.get("model", {}).get("default", "gemini-2.0-flash"),
    }

    # Wire up components
    db_url = os.getenv("DB_URL", "sqlite:///data/monkeybot.db")
    history = ConversationHistory(db_url=db_url)
    await history.init()

    provider = _load_provider()
    inspectors = _load_inspectors(bot_config)

    loop = AgentLoop(provider=provider, history=history, inspectors=inspectors, config=config)
    gateway = CLIGateway(loop=loop, session_id=session_id or str(ulid.new()))
    await gateway.run_interactive()


def _load_provider():
    provider_name = os.getenv("MODEL_PROVIDER", "gemini")
    if provider_name == "gemini":
        from monkeybot.providers.gemini import GeminiProvider
        return GeminiProvider()
    raise ValueError(f"Unknown MODEL_PROVIDER: {provider_name}. Supported: gemini")


def _load_inspectors(bot_config: dict) -> list:
    from monkeybot.core.inspector import RulesInspector
    safety = bot_config.get("safety", {})
    denied = safety.get("denied_patterns", [])
    if denied:
        return [RulesInspector(denied_patterns=denied)]
    return []


if __name__ == "__main__":
    main()
```

---

## Task 4: `tests/unit/test_loop.py`

**Files:** `tests/unit/test_loop.py` (create)

```python
import pytest
from pathlib import Path
from monkeybot.core.events import (
    AssistantDelta, TurnComplete, UserMessage, ToolCallStarted, ToolCallResult, ErrorEvent,
)
from monkeybot.core.history import ConversationHistory
from monkeybot.core.loop import AgentLoop
from monkeybot.core.provider import Message, TextDelta, ToolCall, ProviderDone, ProviderUsage


class FakeProvider:
    name = "fake"
    supports_streaming = True

    def __init__(self, event_batches: list[list]):
        """event_batches: list of provider-event lists, one per stream() call."""
        self._batches = iter(event_batches)

    async def stream(self, messages, tools, *, model, system, context=None):
        batch = next(self._batches)
        async def _gen():
            for event in batch:
                yield event
        return _gen()


@pytest.fixture
async def loop_env(tmp_path: Path):
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# TestBot\nYou are a test bot.")
    (tmp_path / "memory").mkdir()
    (tmp_path / "skills").mkdir()
    db_path = str(tmp_path / "test.db")
    history = ConversationHistory(db_url=f"sqlite:///{db_path}")
    await history.init()
    return tmp_path, history


async def make_loop(tmp_path, history, provider):
    return AgentLoop(
        provider=provider,
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


async def test_simple_text_response(loop_env):
    tmp_path, history = loop_env
    provider = FakeProvider([[
        TextDelta(text="Hello, "),
        TextDelta(text="world!"),
        ProviderDone(usage=ProviderUsage(input_tokens=10, output_tokens=5)),
    ]])
    loop = await make_loop(tmp_path, history, provider)

    events = [e async for e in loop.run("Hi", "sess-1")]

    assert isinstance(events[0], UserMessage)
    text_events = [e for e in events if isinstance(e, AssistantDelta)]
    assert "".join(e.text for e in text_events) == "Hello, world!"
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].input_tokens == 10
    assert events[-1].output_tokens == 5


async def test_turn_complete_always_last_on_error(loop_env):
    """Even if provider raises, TurnComplete must be the final event."""
    tmp_path, history = loop_env

    class BrokenProvider:
        name = "broken"
        supports_streaming = True
        async def stream(self, *a, **kw):
            async def _gen():
                raise RuntimeError("provider exploded")
                yield  # unreachable
            return _gen()

    loop = await make_loop(tmp_path, history, BrokenProvider())
    events = [e async for e in loop.run("Hi", "sess-err")]
    assert isinstance(events[-1], TurnComplete)
    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(error_events) >= 1


async def test_tool_call_cycle(loop_env, tmp_path):
    """Loop re-enters provider after tool call."""
    tmp_path, history = loop_env
    # First stream: ToolCall; second stream: TextDelta + Done
    provider = FakeProvider([
        [
            ToolCall(call_id="c1", name="read_file", args={"path": str(tmp_path / "AGENT.md")}),
            ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=1)),
        ],
        [
            TextDelta(text="Done."),
            ProviderDone(usage=ProviderUsage(input_tokens=8, output_tokens=3)),
        ],
    ])
    loop = await make_loop(tmp_path, history, provider)
    events = [e async for e in loop.run("Read the agent file", "sess-tool")]

    assert any(isinstance(e, ToolCallStarted) for e in events)
    assert any(isinstance(e, ToolCallResult) for e in events)
    assert any(isinstance(e, AssistantDelta) and e.text == "Done." for e in events)
    assert isinstance(events[-1], TurnComplete)


async def test_unknown_tool_returns_error_string(loop_env):
    tmp_path, history = loop_env
    provider = FakeProvider([
        [
            ToolCall(call_id="c1", name="nonexistent_tool", args={}),
            ProviderDone(usage=ProviderUsage(input_tokens=3, output_tokens=1)),
        ],
        [
            TextDelta(text="OK"),
            ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=1)),
        ],
    ])
    loop = await make_loop(tmp_path, history, provider)
    events = [e async for e in loop.run("Use nonexistent tool", "sess-unknown")]
    tool_results = [e for e in events if isinstance(e, ToolCallResult)]
    assert any("Unknown tool" in (e.result or "") for e in tool_results)


async def test_history_persists_after_turn(loop_env):
    tmp_path, history = loop_env
    provider = FakeProvider([[
        TextDelta(text="Response."),
        ProviderDone(usage=ProviderUsage(input_tokens=5, output_tokens=3)),
    ]])
    loop = await make_loop(tmp_path, history, provider)
    [_ async for _ in loop.run("Hello", "sess-persist")]
    msgs = await history.load("sess-persist")
    assert any(m.role == "user" for m in msgs)
    assert any(m.role == "assistant" for m in msgs)
```

---

## Task 5: `tests/test_cold_start.py`

**Files:** `tests/test_cold_start.py` (create)  
**Pattern:** subprocess + wall-clock timing. Copy exact implementation from monkeybot_v2_plan.md Section 20.

```python
import subprocess, time, pytest

def test_import_time():
    start = time.monotonic()
    result = subprocess.run(
        ["python", "-c", "import monkeybot"],
        capture_output=True, timeout=10,
    )
    elapsed_ms = (time.monotonic() - start) * 1000
    assert result.returncode == 0, result.stderr.decode()
    assert elapsed_ms < 200, f"Import took {elapsed_ms:.0f}ms (limit: 200ms)"

def test_cli_startup_time():
    start = time.monotonic()
    result = subprocess.run(
        ["python", "-m", "monkeybot", "--help"],
        capture_output=True, timeout=10,
    )
    elapsed_ms = (time.monotonic() - start) * 1000
    assert result.returncode == 0
    assert elapsed_ms < 500, f"CLI startup took {elapsed_ms:.0f}ms (limit: 500ms)"
```

---

## Final Verification

**Loop contract:**
- [ ] `TurnComplete` is ALWAYS the last event (even on provider exception)
- [ ] `UserMessage` is always the first event
- [ ] Loop re-enters provider after each tool call batch
- [ ] Unknown tool name → `ToolCallResult` with `"Unknown tool: {name}"` in result
- [ ] History has user + assistant messages after a completed turn

**Tool dispatch:**
- [ ] `run_command` called as `await` (async)
- [ ] Sync tools called via `asyncio.to_thread`
- [ ] `allowed_roots` passed to `read_file`/`write_file` from config

**CLI:**
- [ ] `monkeybot run --bot-dir ./bots/example-bot` starts without error
- [ ] `exit` terminates with exit code 0
- [ ] Streaming output printed inline (no buffering)
- [ ] JSON structured logging to stderr

**Cold start:**
- [ ] `test_import_time` passes (< 200ms)
- [ ] `test_cli_startup_time` passes (< 500ms)

**Code Quality:**
- [ ] `ruff check src/` — zero warnings
- [ ] `mypy --strict src/` — zero errors
- [ ] No `Any` in public method signatures without `# type: ignore` comment
