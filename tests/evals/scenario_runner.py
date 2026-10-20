"""Load YAML scenarios and drive :func:`~monkeybot.core.runtime.loop.run` for T2 evals."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from monkeybot.core.coding.verification_gate import VerificationGate
from monkeybot.core.config.settings import VerificationGateConfig
from monkeybot.core.context import TurnContext, _core_tool_defs
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import Done, ProviderEvent, TextDelta, ToolCall
from monkeybot.core.runtime.events import Error, HarnessIntervention, TurnComplete
from monkeybot.core.runtime.loop import run
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.read_ledger import FileReadLedger
from monkeybot.core.tools.types import ToolExecutionResult
from tests.core.test_core_tool_executor import _mem_sub, _NoMCP
from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider, RecordingExecutor
from tests.evals.eval_hook import EvalHook, EvalRecord

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "evals"
_COMMAND_TIMEOUT_SEC = 120


def tool_category(tool_name: str) -> str:
    """Map core tool names to coarse categories (single place to update on renames)."""
    read_tools = frozenset({"read_file", "list_files", "find_files", "list_dir"})
    write_tools = frozenset({"write_file", "create_file", "replace_in_file", "apply_patch"})
    loop_tools = frozenset({"start_loop", "loop_status", "pause_loop", "resume_loop", "stop_loop"})
    code_search_tools = frozenset({"glob", "grep"})
    if tool_name in read_tools:
        return "read"
    if tool_name in write_tools:
        return "write"
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
    # Checked against the real workspace copy (``executor: real`` scenarios only).
    workspace_file_contains: dict[str, str] | None = None
    workspace_command_passes: list[str] | None = None
    # ``details.code`` values that must appear among the tool error envelopes of the run.
    tool_error_codes: list[str] | None = None
    # HarnessIntervention names that must have fired, and the exact number of provider calls.
    interventions: list[str] | None = None
    provider_calls: int | None = None


@dataclass
class Scenario:
    description: str
    user_message: str
    scripted_turns: list[list[ProviderEvent]]
    memory_index: list[str] = field(default_factory=list)
    assertions: ScenarioAssertions = field(default_factory=ScenarioAssertions)
    # ``recording`` answers every tool call with "ok"; ``real`` runs a CoreToolExecutor over a
    # temp copy of ``workspace_fixture`` (a directory under ``evals/``).
    executor: str = "recording"
    workspace_fixture: str | None = None
    # Coding mode for ``executor: real``: the executor gets a read-before-write ledger.
    coding: bool = False
    # Register the turn-end VerificationGate (coding.verification_gate) on the loop's hooks.
    gate: bool = False


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
    commands = assertions_raw.get("workspace_command_passes")
    if isinstance(commands, str):
        commands = [commands]
    assertions = ScenarioAssertions(
        turn_completed=assertions_raw.get("turn_completed"),
        no_errors=assertions_raw.get("no_errors"),
        memory_injected=assertions_raw.get("memory_injected"),
        tool_categories_used=assertions_raw.get("tool_categories_used"),
        max_turns=int(assertions_raw.get("max_turns", 10)),
        workspace_file_contains=assertions_raw.get("workspace_file_contains"),
        workspace_command_passes=commands,
        tool_error_codes=assertions_raw.get("tool_error_codes"),
        interventions=assertions_raw.get("interventions"),
        provider_calls=assertions_raw.get("provider_calls"),
    )
    executor = str(data.get("executor", "recording"))
    if executor not in {"recording", "real"}:
        msg = f"executor must be 'recording' or 'real': {path}"
        raise ValueError(msg)
    fixture = data.get("workspace_fixture")
    if fixture is not None and executor != "real":
        msg = f"workspace_fixture requires executor: real: {path}"
        raise ValueError(msg)
    return Scenario(
        description=str(data.get("description", "")),
        user_message=str(data["user_message"]),
        scripted_turns=_parse_scripted_turns(st_raw),
        memory_index=list(mem),
        assertions=assertions,
        executor=executor,
        workspace_fixture=str(fixture) if fixture else None,
        coding=bool(data.get("coding", False)),
        gate=bool(data.get("gate", False)),
    )


def check_assertions(record: EvalRecord, assertions: ScenarioAssertions) -> None:
    """Raise AssertionError when a set assertion does not hold."""
    if assertions.turn_completed is True:
        assert record.completed
    if assertions.no_errors is True:
        assert record.errors == []
    if assertions.tool_categories_used:
        cats = {tool_category(name) for name in record.tool_calls}
        assert cats.intersection(set(assertions.tool_categories_used))
    if assertions.memory_injected is True:
        assert len(record.memory_injected_lines) > 0
    if assertions.interventions:
        missing_iv = [i for i in assertions.interventions if i not in record.interventions]
        assert not missing_iv, f"interventions {missing_iv} not seen (saw {record.interventions})"
    if assertions.provider_calls is not None:
        assert record.provider_calls == assertions.provider_calls, record.provider_calls
    if assertions.tool_error_codes:
        seen = _error_codes(record)
        missing = [c for c in assertions.tool_error_codes if c not in seen]
        assert not missing, f"tool error codes {missing} not seen (saw {sorted(seen)})"
    if assertions.workspace_file_contains or assertions.workspace_command_passes:
        assert record.workspace is not None, "workspace assertions need executor: real"
    for rel, needle in (assertions.workspace_file_contains or {}).items():
        assert record.workspace is not None
        text = (record.workspace / rel).read_text(encoding="utf-8")
        assert needle in text, f"{rel} does not contain {needle!r}"
    for command in assertions.workspace_command_passes or []:
        assert record.workspace is not None
        passed, detail = run_workspace_command(command, record.workspace)
        assert passed, f"`{command}` failed: {detail}"


def _error_codes(record: EvalRecord) -> set[str]:
    codes: set[str] = set()
    for _name, text in record.tool_results:
        try:
            envelope = json.loads(text)
        except ValueError:
            continue
        if isinstance(envelope, dict) and envelope.get("ok") is False:
            details = envelope.get("details")
            if isinstance(details, dict) and isinstance(details.get("code"), str):
                codes.add(details["code"])
    return codes


def run_workspace_command(command: str, cwd: Path) -> tuple[bool, str]:
    """Run ``command`` (argv-split, no shell) in ``cwd``; ``python`` means this interpreter."""
    argv = shlex.split(command)
    if argv and argv[0] in {"python", "python3"}:
        argv[0] = sys.executable
    proc = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_COMMAND_TIMEOUT_SEC,
        check=False,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()[-400:]


class _ObservingExecutor:
    """Delegates to a real executor while recording each call and its result text."""

    def __init__(self, inner: CoreToolExecutor, record: EvalRecord) -> None:
        self._inner = inner
        self._record = record
        self.calls: list[ToolCall] = []

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
        self.calls.append(call)
        result = await self._inner.execute(call=call, ctx=ctx)
        self._record.tool_results.append((call.name, result.error or _result_text(result)))
        return result


def _result_text(result: ToolExecutionResult) -> str:
    return "\n".join(getattr(block, "text", "") for block in result.blocks)


def _prepare_real_workspace(scenario: Scenario, root: Path) -> tuple[Path, CoreToolExecutor]:
    workspace = root / "workspace"
    if scenario.workspace_fixture:
        shutil.copytree(FIXTURES_ROOT / scenario.workspace_fixture, workspace)
    else:
        workspace.mkdir()
    memory_dir = root / "mem"
    skills_dir = root / "skills"
    memory_dir.mkdir()
    skills_dir.mkdir()
    executor = CoreToolExecutor(
        workspace_root=workspace,
        memory=_mem_sub(memory_dir),
        skills_path=skills_dir,
        mcp=_NoMCP(),
        read_ledger=FileReadLedger() if scenario.coding else None,
    )
    return workspace, executor


def _make_turn_context(scenario: Scenario, workspace: Path | None = None) -> TurnContext:
    tools = _core_tool_defs(include_task_tool=True)
    return TurnContext(
        workspace_root=workspace,
        thread_id="eval-t2-thread",
        request_id="eval-t2-req",
        agent_md="# Eval agent\n",
        memory_index=list(scenario.memory_index),
        skills=[],
        tools=tools,
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )


async def run_scenario(scenario: Scenario, workspace_root: Path | None = None) -> EvalRecord:
    """Run one scenario through the real loop with a scripted fake provider.

    ``executor: real`` scenarios need ``workspace_root`` (an empty temp directory): the fixture
    is copied under it and the loop's tool calls execute against that copy.
    """
    record = EvalRecord()
    hook = EvalHook(record)
    mgr = HookManager()

    if scenario.memory_index:

        async def _seed_pre_turn(p: HookPayload) -> None:
            p.inject_memory_lines.extend(scenario.memory_index)

        mgr.register(HookEvent.PRE_TURN, _seed_pre_turn)

    hook.register(mgr)
    if scenario.gate:
        VerificationGate(VerificationGateConfig(enabled=True)).register(mgr)

    provider = FakeProvider(scenario.scripted_turns)
    workspace: Path | None = None
    executor: RecordingExecutor | _ObservingExecutor
    if scenario.executor == "real":
        if workspace_root is None:
            msg = "executor: real scenarios need a workspace_root"
            raise ValueError(msg)
        workspace, real = _prepare_real_workspace(scenario, workspace_root)
        executor = _ObservingExecutor(real, record)
        record.workspace = workspace
    else:
        executor = RecordingExecutor()
    history = FakeHistory()
    ctx = _make_turn_context(scenario, workspace)
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
        if isinstance(evt, HarnessIntervention):
            record.interventions.append(evt.intervention)
    # POST_TOOL hooks are scheduled in the background; tool names are reliable
    # from the executor the loop awaited.
    record.tool_calls = [c.name for c in executor.calls]
    record.provider_calls = provider.stream_calls
    return record
