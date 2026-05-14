"""Agent loop — orchestrates a single turn."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ulid

from monkeybot.core.context import load_turn_context
from monkeybot.core.events import (
    AgentEvent,
    AssistantDelta,
    ErrorEvent,
    SubagentCompleted,
    SubagentStarted,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
    UserMessage,
)
from monkeybot.core.history import ConversationHistory
from monkeybot.core.inspector import Decision, ToolInspector
from monkeybot.core.provider import Message, Provider, ProviderDone, TextDelta, ToolCall

if TYPE_CHECKING:
    from monkeybot.core.durable_runs import DurableRunStore
    from monkeybot.core.subagent_registry import SubagentRegistry

log = logging.getLogger("monkeybot.loop")

_SPAWN_SUBAGENT_TOOL_DEF = {
    "name": "spawn_subagent",
    "description": (
        "Spawn a registered subagent to handle a specialized task. "
        "Use 'name' for a registry-defined subagent or 'script' for an ad-hoc script path. "
        "The subagent runs in isolation and its events are streamed back."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Registry name of the subagent (see Available Subagents list)",
            },
            "script": {
                "type": "string",
                "description": "Path to a Python subagent script for ad-hoc spawns",
            },
            "task": {
                "type": "string",
                "description": "Natural language task description for the subagent",
            },
            "context": {
                "type": "object",
                "description": "Optional key-value context to pass to the subagent",
            },
        },
        "required": ["task"],
    },
}


class AgentLoop:
    """Orchestrates a single agent turn: load context → stream → dispatch tools → repeat."""

    def __init__(
        self,
        provider: Provider,
        history: ConversationHistory,
        inspectors: list[ToolInspector],
        config: dict[str, Any],
        on_turn_complete: Callable[[str, TurnComplete], Awaitable[None]] | None = None,
        registry: SubagentRegistry | None = None,
        durable_store: DurableRunStore | None = None,
    ) -> None:
        self._provider = provider
        self._history = history
        self._inspectors = inspectors
        self._config = config
        self._on_turn_complete = on_turn_complete
        self._registry = registry
        self._durable_store = durable_store
        self._tools = self._build_tool_registry()

    def _build_tool_registry(self) -> dict[str, Any]:
        """Hard-coded 5-tool registry. Returns {name: callable}."""
        from monkeybot.tools.file_ops import read_file, write_file
        from monkeybot.tools.memory_ops import search_memory
        from monkeybot.tools.run_command import run_command
        from monkeybot.tools.skill_ops import list_skills

        memory_path = str(self._config.get("memory_path", "./data/memory"))
        skills_path = str(self._config.get("skills_path", "./.agents/skills"))
        bot_dir = str(self._config.get("bot_dir", "."))
        allowed_roots = [
            Path(memory_path).expanduser().resolve(),
            Path(bot_dir).expanduser().resolve(),
            Path(skills_path).expanduser().resolve(),
        ]

        from monkeybot.tools.memory_ops import save_memory

        return {
            "run_command": lambda args: run_command(**args),
            "read_file": lambda args: read_file(**{**args, "allowed_roots": allowed_roots}),
            "write_file": lambda args: write_file(**{**args, "allowed_roots": allowed_roots}),
            "save_memory": lambda args: save_memory(**{**args, "memory_path": memory_path}),
            "search_memory": lambda args: search_memory(**{**args, "memory_path": memory_path}),
            "list_skills": lambda args: list_skills(**{**args, "skills_path": skills_path}),
        }

    def _get_tool_defs(self) -> list[Any]:
        """Build ToolDef list from registered tool modules plus spawn_subagent."""
        from monkeybot.core.provider import ToolDef
        from monkeybot.tools import file_ops, memory_ops, skill_ops
        from monkeybot.tools import run_command as rc_mod

        all_defs: list[dict[str, Any]] = [
            rc_mod.TOOL_DEF,
            *file_ops.TOOL_DEFS,
            *memory_ops.TOOL_DEFS,
            skill_ops.TOOL_DEF,
            _SPAWN_SUBAGENT_TOOL_DEF,
        ]
        defs = []
        for tool_def_dict in all_defs:
            defs.append(
                ToolDef(
                    name=tool_def_dict["name"],
                    description=tool_def_dict["description"],
                    parameters=tool_def_dict["parameters"],
                )
            )
        return defs

    async def _dispatch_spawn_subagent(
        self,
        call: ToolCall,
        ctx: Any,
        events: list[AgentEvent],
    ) -> tuple[str, list[AgentEvent]]:
        """Handle spawn_subagent tool call, recording lifecycle in DurableRunStore."""
        from monkeybot.core.subagent_proto import SubagentDefinition, spawn_subagent

        name = call.args.get("name")
        script = call.args.get("script")
        task = str(call.args.get("task", ""))
        context: dict[str, Any] = call.args.get("context") or {}

        definition_or_script: SubagentDefinition | str
        if name and self._registry is not None:
            try:
                definition_or_script = self._registry.resolve(str(name))
            except KeyError as exc:
                result = f"ERROR: {exc}"
                events.append(
                    ToolCallResult(call_id=call.call_id, tool_name=call.name, result=result)
                )
                return result, events
        elif script:
            definition_or_script = str(script)
        else:
            result = "ERROR: spawn_subagent requires 'name' (registry) or 'script' (path)"
            events.append(
                ToolCallResult(call_id=call.call_id, tool_name=call.name, result=result)
            )
            return result, events

        parent_run_id = str(ctx.run_id) if getattr(ctx, "run_id", None) else None
        active_run_id: str | None = None
        scratch_dir = ""
        completed_cleanly = False

        async for ev in spawn_subagent(
            definition_or_script, task, context, parent_run_id=parent_run_id
        ):
            events.append(ev)
            if isinstance(ev, SubagentStarted):
                active_run_id = ev.run_id
                if self._durable_store is not None:
                    agent_name = (
                        definition_or_script.name
                        if isinstance(definition_or_script, SubagentDefinition)
                        else None
                    )
                    await self._durable_store.record_started(
                        ev.run_id,
                        ev.script,
                        "",
                        parent_run_id=ev.parent_run_id,
                        agent_name=agent_name,
                    )
            elif isinstance(ev, SubagentCompleted):
                scratch_dir = ev.scratch_dir
                completed_cleanly = True
                if self._durable_store is not None and active_run_id:
                    await self._durable_store.record_completed(active_run_id)
            elif isinstance(ev, ErrorEvent) and not ev.recoverable:
                if self._durable_store is not None and active_run_id:
                    await self._durable_store.record_failed(active_run_id, ev.message)

        if not completed_cleanly and self._durable_store is not None and active_run_id:
            await self._durable_store.record_failed(active_run_id, "subagent did not complete")

        result = f"Subagent done. run_id={active_run_id}, scratch_dir={scratch_dir}"
        events.append(
            ToolCallResult(call_id=call.call_id, tool_name=call.name, result=result)
        )
        return result, events

    async def _run_inspectors(self, call: ToolCall, ctx: Any) -> Decision:
        """Run inspector chain. Returns first non-allow decision, or allow."""
        for inspector in self._inspectors:
            decision = await inspector.check(call, ctx)
            if decision.kind != "allow":
                return decision
        return Decision(kind="allow")

    async def _dispatch_tool(self, call: ToolCall, ctx: Any) -> tuple[str, list[AgentEvent]]:
        """Dispatch a tool call. Returns (result_str, events_to_yield)."""
        events: list[AgentEvent] = []

        decision = await self._run_inspectors(call, ctx)
        if decision.kind == "deny":
            err = f"Denied: {decision.message}"
            events.append(
                ToolCallResult(
                    call_id=call.call_id,
                    tool_name=call.name,
                    result="",
                    error=err,
                )
            )
            return f"ERROR: Tool denied: {decision.message}", events

        events.append(
            ToolCallStarted(
                call_id=call.call_id,
                tool_name=call.name,
                args=call.args,
            )
        )

        if call.name == "spawn_subagent":
            return await self._dispatch_spawn_subagent(call, ctx, events)

        callable_ = self._tools.get(call.name)
        if callable_ is None:
            result = f"Unknown tool: {call.name}"
            events.append(
                ToolCallResult(
                    call_id=call.call_id,
                    tool_name=call.name,
                    result=result,
                )
            )
            return result, events

        start = time.monotonic()
        result = ""
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
        events.append(
            ToolCallResult(
                call_id=call.call_id,
                tool_name=call.name,
                result=result,
                duration_ms=duration_ms,
            )
        )
        return result, events

    async def run(
        self,
        user_message: str,
        session_id: str,
        user_id: str | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Execute a full agent turn. Always yields TurnComplete as the last event."""
        run_id = str(ulid.new())
        start_ms = int(time.monotonic() * 1000)
        input_tokens = 0
        output_tokens = 0

        yield UserMessage(content=user_message, user_id=user_id)

        try:
            history_msgs = await self._history.load(session_id)
            await self._history.save(session_id, "user", user_message)

            ctx = load_turn_context(
                agent_md_path=self._config["agent_md_path"],
                memory_path=str(self._config.get("memory_path", "./data/memory")),
                skills_path=str(self._config.get("skills_path", "./.agents/skills")),
                user_id=user_id,
                run_id=run_id,
            )

            messages: list[Message] = list(history_msgs) + [
                Message(role="user", content=user_message)
            ]
            tool_defs = self._get_tool_defs()
            full_response_parts: list[str] = []

            system_prompt = ctx.build_system_prompt()
            if self._registry is not None:
                registry_block = self._registry.to_prompt_block()
                if registry_block:
                    system_prompt += "\n\n" + registry_block

            max_tool_iterations = int(self._config.get("max_tool_iterations", 3))
            tool_iteration = 0

            while True:
                tool_called_this_iteration = False

                async for pev in await self._provider.stream(
                    messages,
                    tool_defs,
                    model=str(self._config.get("model", "gemini-2.0-flash")),
                    system=system_prompt,
                    context=ctx,
                ):
                    if isinstance(pev, TextDelta):
                        yield AssistantDelta(text=pev.text)
                        full_response_parts.append(pev.text)

                    elif isinstance(pev, ToolCall):
                        tool_called_this_iteration = True
                        log.info(
                            "tool_call",
                            extra={
                                "run_id": run_id,
                                "tool": pev.name,
                                "tool_args": str(pev.args)[:200],
                                "iteration": tool_iteration,
                            },
                        )
                        result_str, tool_events = await self._dispatch_tool(pev, ctx)
                        log.info(
                            "tool_result",
                            extra={
                                "run_id": run_id,
                                "tool": pev.name,
                                "result": result_str[:200],
                            },
                        )
                        for evt in tool_events:
                            yield evt
                        messages.append(
                            Message(role="assistant", content="", tool_call_id=pev.call_id)
                        )
                        messages.append(
                            Message(
                                role="tool",
                                content=result_str,
                                tool_call_id=pev.call_id,
                                tool_name=pev.name,
                            )
                        )

                    elif isinstance(pev, ProviderDone):
                        input_tokens += pev.usage.input_tokens
                        output_tokens += pev.usage.output_tokens

                if not tool_called_this_iteration:
                    break

                tool_iteration += 1
                if tool_iteration >= max_tool_iterations:
                    log.warning(
                        "max_tool_iterations_reached",
                        extra={"run_id": run_id, "limit": max_tool_iterations},
                    )
                    yield ErrorEvent(
                        message=f"Turn aborted: exceeded {max_tool_iterations} tool-call iterations.",
                        recoverable=True,
                    )
                    break

            full_response = "".join(full_response_parts)
            if full_response:
                await self._history.save(session_id, "assistant", full_response)

        except Exception as exc:
            yield ErrorEvent(message=str(exc), recoverable=True)
            log.error("Loop error in run_id=%s: %s", run_id, exc, exc_info=True)

        finally:
            duration_ms = int(time.monotonic() * 1000) - start_ms
            tc = TurnComplete(
                run_id=run_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
            )
            yield tc
            if self._on_turn_complete is not None:
                try:
                    await self._on_turn_complete(session_id, tc)
                except Exception:
                    log.exception("on_turn_complete callback failed run_id=%s", run_id)
            log.info(
                "turn_complete",
                extra={
                    "session_id": session_id,
                    "run_id": run_id,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "duration_ms": duration_ms,
                },
            )
