"""Load YAML scenarios and drive :func:`~monkeybot.core.runtime.loop.run` for T2 evals."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from monkeybot.core.context import TurnContext, _core_tool_defs
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import Done, ProviderEvent, TextDelta, ToolCall
from monkeybot.core.runtime.events import Error, TurnComplete
from monkeybot.core.runtime.loop import run
from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider, RecordingExecutor
from tests.evals.eval_hook import EvalHook, EvalRecord


def tool_category(tool_name: str) -> str:
    """Map core tool names to coarse categories (single place to update on renames)."""
    read_tools = frozenset({"read_file", "list_files", "find_files"})
    write_tools = frozenset({"write_file", "create_file", "replace_in_file", "apply_patch"})
    loop_tools = frozenset(
        {"start_loop", "loop_status", "pause_loop", "resume_loop", "stop_loop"}
    )
    code_search_tools = frozenset({"glob", "grep"})
    if tool_name in read_tools:
        return "read"
    if tool_name in write_tools:
        return "write"
    if tool_name == "search_memory":
        return "memory"
    if tool_name == "run_command":
        return "shell"
    if tool_name == "task":
        return "subagent"
    if tool_name in loop_tools:
        return "loop"
    if tool_name == "web_search":
        return "web_search"
    if tool_name == "todo_list":
        return "todo"
    if tool_name == "load_file":
        return "attachment"
    if tool_name == "search":
        return "knowledge_search"
    if tool_name in code_search_tools:
        return "code_search"
    return "other"


@dataclass
class ScenarioAssertions:
    turn_completed: bool | None = None
    no_errors: bool | None = None
    memory_injected: bool | None = None
    tool_categories_used: list[str] | None = None
    max_turns: int = 10


@dataclass
class Scenario:
    description: str
    user_message: str
    scripted_turns: list[list[ProviderEvent]]
    memory_index: list[str] = field(default_factory=list)
    assertions: ScenarioAssertions = field(default_factory=ScenarioAssertions)


def _event_from_dict(raw: dict[str, Any]) -> ProviderEvent:
    t = raw["type"]
    if t == "text_delta":
        return TextDelta(text=str(raw["text"]))
    if t == "done":
        return Done()
    if t == "tool_call":
        return ToolCall(
            call_id=str(raw["call_id"]),
            name=str(raw["name"]),
            args=dict(raw.get("args") or {}),
        )
    msg = f"Unknown scripted event type: {t!r}"
    raise ValueError(msg)


def _parse_scripted_turns(raw: list[Any]) -> list[list[ProviderEvent]]:
    out: list[list[ProviderEvent]] = []
    for turn in raw:
        if not isinstance(turn, list):
            msg = f"Each scripted turn must be a list, got {type(turn)}"
            raise TypeError(msg)
        out.append([_event_from_dict(cast(dict[str, Any], d)) for d in turn])
    return out


def load_scenario(path: Path) -> Scenario:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"Scenario root must be a mapping: {path}"
        raise ValueError(msg)
    assertions_raw = data.get("assertions") or {}
    if not isinstance(assertions_raw, dict):
        msg = f"assertions must be a mapping: {path}"
        raise ValueError(msg)
    st_raw = data["scripted_turns"]
    if not isinstance(st_raw, list):
        msg = f"scripted_turns must be a list: {path}"
        raise TypeError(msg)
    mem = data.get("memory_index") or []
    if not isinstance(mem, list) or not all(isinstance(x, str) for x in mem):
        msg = f"memory_index must be a list of strings: {path}"
        raise TypeError(msg)
    assertions = ScenarioAssertions(
        turn_completed=assertions_raw.get("turn_completed"),
        no_errors=assertions_raw.get("no_errors"),
        memory_injected=assertions_raw.get("memory_injected"),
        tool_categories_used=assertions_raw.get("tool_categories_used"),
        max_turns=int(assertions_raw.get("max_turns", 10)),
    )
    return Scenario(
        description=str(data.get("description", "")),
        user_message=str(data["user_message"]),
        scripted_turns=_parse_scripted_turns(st_raw),
        memory_index=list(mem),
        assertions=assertions,
    )


def _make_turn_context(scenario: Scenario) -> TurnContext:
    tools = _core_tool_defs(include_task_tool=True)
    return TurnContext(
        thread_id="eval-t2-thread",
        request_id="eval-t2-req",
        agent_md="# Eval agent\n",
        memory_index=list(scenario.memory_index),
        skills=[],
        tools=tools,
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
        context_curation_enabled=False,
    )


async def run_scenario(scenario: Scenario) -> EvalRecord:
    """Run one scenario through the real loop with a scripted fake provider."""
    record = EvalRecord()
    hook = EvalHook(record)
    mgr = HookManager()

    if scenario.memory_index:

        async def _seed_pre_turn(p: HookPayload) -> None:
            p.inject_memory_lines.extend(scenario.memory_index)

        mgr.register(HookEvent.PRE_TURN, _seed_pre_turn)

    hook.register(mgr)

    provider = FakeProvider(scenario.scripted_turns)
    executor = RecordingExecutor()
    history = FakeHistory()
    ctx = _make_turn_context(scenario)
    events: list[object] = []
    async for evt in run(
        scenario.user_message,
        ctx,
        provider=provider,
        history=history,
        inspectors=[AllowInspector()],
        tool_executor=executor,
        hook_manager=mgr,
        max_turns=scenario.assertions.max_turns,
    ):
        events.append(evt)
        if isinstance(evt, TurnComplete):
            record.completed = True
            record.trace_id = evt.trace_id
        if isinstance(evt, Error):
            record.errors.append(evt.error)
    # POST_TOOL hooks are scheduled in the background; tool names are reliable
    # from the executor the loop awaited.
    record.tool_calls = [c.name for c in executor.calls]
    return record
