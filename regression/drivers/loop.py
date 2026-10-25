"""Drive :func:`monkeybot.core.runtime.loop.run` for one scripted scenario.

The provider, history, inspector, and recording executor are the testkit
doubles. ``executor: real`` uses the real :class:`CoreToolExecutor`. Hook
bundles registered here are ``verification_gate`` and ``memory``; any other
bundle name fails instead of being skipped. Invariants and the scenario's
assertions run before this function returns.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from monkeybot.core.coding.verification_gate import VerificationGate
from monkeybot.core.config.runtime_env import _deep_merge, _flatten_config
from monkeybot.core.config.settings import ConfigError
from monkeybot.core.config.snapshot import (
    RuntimeConfig,
    _snapshot_with_env,
    build_runtime_config,
    context_window_tokens,
)
from monkeybot.core.context import TurnContext, _core_tool_defs
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import ProviderEvent, ToolCall
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.runtime.loop import run
from monkeybot.core.testing.mocks_provider import RaiseStep, _exception_class
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.read_ledger import FileReadLedger
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.content_blocks import Text
from regression.drivers.record import EvalHook, EvalRecord
from regression.schema.assertions import ProviderRequest, RunView, evaluate
from regression.schema.events import RaiseEvent, to_provider_event
from regression.schema.invariants import check_invariants
from regression.schema.scenario import PalaceDrawer, Scenario
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory
from testkit.inspectors import AllowInspector
from testkit.mcp import NoMCP
from testkit.palace import InMemoryPalace
from testkit.provider import ScriptedProvider

_REPO = Path(__file__).resolve().parents[2]
_BASE_CONFIG = _REPO / "regression" / "fixtures" / "configs" / "agent.base.yaml"
_WORKSPACE_ROOTS = (
    _REPO / "regression" / "fixtures" / "workspaces",
    _REPO / "evals" / "fixtures" / "coding",
)
_FIXTURE_PREFIXES = (
    "regression/fixtures/workspaces/",
    "evals/fixtures/coding/",
    "fixtures/coding/",
)
_IMPLEMENTED_HOOKS = frozenset({"verification_gate", "memory"})


class LoopDriverError(RuntimeError):
    """The loop driver cannot run this scenario as written."""


@dataclass
class LoopRun:
    """Events the loop yielded, and the tool calls the executor recorded."""

    events: list[object]
    calls: list[ToolCall]
    view: RunView


async def run_scenario(scenario: Scenario) -> LoopRun:
    """Run ``scenario`` through the real loop, then invariants and assertions.

    The temporary workspace is removed when this function returns. Checks run
    while it still exists. A failed invariant or assertion raises
    :class:`AssertionError`.
    """
    _reject_unsupported(scenario)
    with tempfile.TemporaryDirectory(prefix="mb-loop-") as raw:
        return await _run(scenario, Path(raw))


def _reject_unsupported(scenario: Scenario) -> None:
    if scenario.mode != "scripted":
        raise LoopDriverError(f"loop driver runs scripted scenarios, not mode {scenario.mode!r}")
    if scenario.driver not in (None, "loop"):
        raise LoopDriverError(f"loop driver cannot run driver {scenario.driver!r}")
    if not scenario.messages:
        raise LoopDriverError("scripted loop scenario needs at least one message")
    if scenario.sessions is not None:
        raise LoopDriverError("loop driver does not run sessions yet; set messages")
    if not scenario.script:
        raise LoopDriverError("scripted loop scenario is missing script")
    if scenario.steps:
        raise LoopDriverError(
            f"loop driver does not run steps yet ({len(scenario.steps)} step(s)); that is P1-03"
        )
    if scenario.policy is not None:
        raise LoopDriverError("loop driver does not load policy fixtures yet")
    missing = [name for name in _hook_names(scenario) if name not in _IMPLEMENTED_HOOKS]
    if missing:
        joined = ", ".join(missing)
        raise LoopDriverError(
            f"loop driver does not register hook bundle(s): {joined}. "
            "P0B-04 registers verification_gate and memory only"
        )
    drawers = scenario.memory.palace if scenario.memory is not None else []
    if drawers and "memory" not in scenario.hooks:
        raise LoopDriverError(
            "memory.palace is set but hooks does not include memory; "
            "the loop driver will not drop the palace seed"
        )


def _hook_names(scenario: Scenario) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for name in scenario.hooks:
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


async def _run(scenario: Scenario, workdir: Path) -> LoopRun:
    cfg, env = _scenario_config(scenario, workdir)
    hook_names = _hook_names(scenario)
    if "verification_gate" in hook_names:
        cfg = _with_gate_enabled(cfg)
    workspace, pristine = _materialize_workspace(scenario, workdir)
    messages = _user_messages(scenario, workspace)
    drawers = list(scenario.memory.palace) if scenario.memory is not None else []
    hook_memory = _memory_subsystem(workdir / "memory", drawers) if "memory" in hook_names else None
    read_ledger: FileReadLedger | None = None
    executor: RecordingExecutor | _ObservedExecutor
    if scenario.executor == "real":
        if workspace is None:
            raise LoopDriverError("executor real needs a workspace directory")
        skills = workdir / "skills"
        skills.mkdir()
        read_ledger = FileReadLedger()
        tool_memory = hook_memory or _memory_subsystem(workdir / "tool-memory", [])
        executor = _ObservedExecutor(
            CoreToolExecutor(
                workspace_root=workspace,
                memory=tool_memory,
                skills_path=skills,
                mcp=NoMCP(),
                read_ledger=read_ledger,
                config=cfg,
            )
        )
    else:
        executor = _recording_executor(scenario)

    record = EvalRecord()
    manager = _hooks(scenario, hook_names, hook_memory, cfg, record)
    provider = ScriptedProvider(_script_turns(scenario))
    history = FakeHistory(list(scenario.history))
    try:
        with _pushed_env(env):
            events = await _drive(
                messages,
                provider=provider,
                history=history,
                executor=executor,
                manager=manager,
                cfg=cfg,
                workspace=workspace,
                read_ledger=read_ledger,
                hook_memory=hook_memory,
            )
            view = _view(
                scenario,
                events=events,
                workspace=workspace,
                pristine=pristine,
                provider=provider,
                history=history,
                record=record,
            )
            _check(scenario, view)
            return LoopRun(events=events, calls=list(executor.calls), view=view)
    finally:
        await _shutdown(executor, hook_memory)


def _scenario_config(scenario: Scenario, workdir: Path) -> tuple[RuntimeConfig, dict[str, str]]:
    """Merge ``config`` over the base file and return the snapshot plus ``env``."""
    if not _BASE_CONFIG.is_file():
        raise LoopDriverError(f"base agent config is missing: {_BASE_CONFIG}")
    loaded = yaml.safe_load(_BASE_CONFIG.read_text(encoding="utf-8"))
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise LoopDriverError("agent.base.yaml root must be a mapping")
    overrides, env = _config_overrides(scenario)
    merged = _merge_overrides(loaded, overrides)
    config_path = workdir / "monkeybot.yaml"
    config_path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
    try:
        cfg = build_runtime_config(config_path=config_path, agent_root=workdir)
    except (ConfigError, ValueError, OSError, yaml.YAMLError) as exc:
        raise LoopDriverError(f"could not build scenario config: {exc}") from exc
    # The merged file is the scenario's config. Ambient process pins must not
    # hide it. ``config.env`` then wins over that file, matching env-over-YAML.
    overlay = dict(cfg.env_values)
    overlay.update(_flatten_config(merged))
    overlay.update(env)
    if overlay != dict(cfg.env_values):
        cfg = _snapshot_with_env(cfg, overlay, base_digest=cfg.digest)
    return cfg, env


def _config_overrides(scenario: Scenario) -> tuple[dict[str, Any], dict[str, str]]:
    if scenario.config is None:
        return {}, {}
    dumped = scenario.config.model_dump()
    raw_env = dumped.pop("env", {})
    env = dict(raw_env) if isinstance(raw_env, dict) else {}
    return dumped, env


def _merge_overrides(base: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = _deep_merge({}, dict(base))
    for key, value in overrides.items():
        if "." in key or not isinstance(value, dict):
            _assign_dotted(merged, key, value)
            continue
        current = merged.get(key)
        if isinstance(current, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _assign_dotted(doc: dict[str, Any], dotted: str, value: object) -> None:
    parts = [part for part in dotted.split(".") if part]
    if not parts:
        raise LoopDriverError(f"config override {dotted!r} is empty")
    cursor = doc
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value


def _with_gate_enabled(cfg: RuntimeConfig) -> RuntimeConfig:
    gate = replace(cfg.coding.verification_gate, enabled=True)
    return replace(cfg, coding=replace(cfg.coding, verification_gate=gate))


def resolve_workspace(name: str) -> Path:
    """Find a fixture under the regression tree, then ``evals/fixtures/coding``.

    ``calc_repo``, ``calc_repo_green``, ``shop_repo``, and ``overlays/...``
    still live in the evals tree until P4-01. A ``fixtures/coding/`` prefix
    is stripped so either spelling resolves.
    """
    relative = name.strip().replace("\\", "/")
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise LoopDriverError(f"workspace path must stay inside the fixture trees: {name!r}")
    for prefix in _FIXTURE_PREFIXES:
        if relative.startswith(prefix):
            relative = relative[len(prefix) :]
            break
    if not relative or ".." in Path(relative).parts:
        raise LoopDriverError(f"workspace path must stay inside the fixture trees: {name!r}")
    for root in _WORKSPACE_ROOTS:
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.is_dir():
            return candidate
    searched = ", ".join(str(root) for root in _WORKSPACE_ROOTS)
    raise LoopDriverError(f"workspace fixture {name!r} was not found under {searched}")


def _materialize_workspace(scenario: Scenario, workdir: Path) -> tuple[Path | None, Path | None]:
    wants_tree = scenario.workspace_fixture is not None or bool(scenario.workspace_overlays)
    if not wants_tree and scenario.executor != "real":
        return None, None
    workspace = workdir / "workspace"
    if scenario.workspace_fixture is not None:
        shutil.copytree(resolve_workspace(scenario.workspace_fixture), workspace)
    else:
        workspace.mkdir()
    for overlay_name in scenario.workspace_overlays:
        shutil.copytree(resolve_workspace(overlay_name), workspace, dirs_exist_ok=True)
    if not wants_tree:
        return workspace, None
    pristine = workdir / "pristine"
    shutil.copytree(workspace, pristine)
    return workspace, pristine


def _user_messages(scenario: Scenario, workspace: Path | None) -> list[str]:
    messages = list(scenario.messages)
    if workspace is None:
        return messages
    name = workspace.name
    return [message.replace("{workspace_subdir}", name) for message in messages]


def _memory_subsystem(root: Path, drawers: Sequence[PalaceDrawer]) -> MemorySubsystem:
    root.mkdir(parents=True, exist_ok=True)
    palace_path = root / "mempalace"
    palace = InMemoryPalace(palace_path, agent_name="loop-scenario")
    for drawer in drawers:
        metadata = dict(drawer.metadata)
        metadata.setdefault("wing", drawer.wing)
        metadata.setdefault("room", drawer.room)
        if drawer.filed_at is not None:
            metadata["filed_at"] = drawer.filed_at
        palace.upsert_drawer(drawer.drawer_id, drawer.content, metadata)
    return MemorySubsystem(
        memory_uri=f"local://{palace_path}",
        db_url=f"sqlite:///{root / 'monkeybot.db'}",
        agent_id="loop-scenario",
        agent_name="loop-scenario",
        palace=palace,
        writer_enabled=False,
    )


def _recording_executor(scenario: Scenario) -> RecordingExecutor:
    if scenario.tool_results is None:
        return RecordingExecutor()
    return _MappedRecording(scenario.tool_results)


class _MappedRecording(RecordingExecutor):
    """Recording executor that answers each call id from ``tool_results``."""

    def __init__(self, envelopes: dict[str, dict[str, Any]]) -> None:
        super().__init__()
        self._envelopes = envelopes

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
        del ctx
        self.calls.append(call)
        envelope = self._envelopes.get(call.call_id)
        if envelope is None:
            return self.result
        try:
            text = json.dumps(envelope)
        except TypeError as exc:
            raise LoopDriverError(f"tool_results[{call.call_id}] is not JSON-serializable") from exc
        return ToolExecutionResult.ok_text(text)


class _ObservedExecutor:
    """Records calls while the real executor runs them."""

    def __init__(self, inner: CoreToolExecutor) -> None:
        self._inner = inner
        self.calls: list[ToolCall] = []

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
        self.calls.append(call)
        return await self._inner.execute(call=call, ctx=ctx)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _hooks(
    scenario: Scenario,
    hook_names: list[str],
    hook_memory: MemorySubsystem | None,
    cfg: RuntimeConfig,
    record: EvalRecord,
) -> HookManager:
    manager = HookManager()
    index = list(scenario.memory.index) if scenario.memory is not None else []
    if index:
        manager.register(HookEvent.PRE_TURN, _index_hook(index))
    if hook_memory is not None:
        hook_memory.register_hooks(manager)
    if "verification_gate" in hook_names:
        VerificationGate(cfg.coding.verification_gate).register(manager)
    # Last, so it sees memory lines the earlier PRE_TURN hooks injected.
    EvalHook(record).register(manager)
    return manager


def _index_hook(lines: list[str]) -> Any:
    async def _inject(payload: HookPayload) -> None:
        payload.inject_memory_lines.extend(lines)

    return _inject


def _script_turns(scenario: Scenario) -> list[list[ProviderEvent | RaiseStep]]:
    if scenario.script is None:
        raise LoopDriverError("scripted loop scenario is missing script")
    turns: list[list[ProviderEvent | RaiseStep]] = []
    for index, turn in enumerate(scenario.script):
        compiled: list[ProviderEvent | RaiseStep] = []
        where = f"{scenario.case} script turn {index + 1}"
        for event in turn:
            if isinstance(event, RaiseEvent):
                compiled.append(
                    RaiseStep(
                        error=_exception_class(event.error, where=where),
                        message=event.message or "",
                        after=event.after,
                    )
                )
            else:
                compiled.append(to_provider_event(event))
        turns.append(compiled)
    return turns


async def _drive(
    messages: list[str],
    *,
    provider: ScriptedProvider,
    history: FakeHistory,
    executor: RecordingExecutor | _ObservedExecutor,
    manager: HookManager,
    cfg: RuntimeConfig,
    workspace: Path | None,
    read_ledger: FileReadLedger | None,
    hook_memory: MemorySubsystem | None,
) -> list[object]:
    events: list[object] = []
    ctx = _turn_context(
        cfg,
        workspace=workspace,
        read_ledger=read_ledger,
        hook_memory=hook_memory,
        request_id="loop-scenario",
    )
    for nth, message in enumerate(messages, start=1):
        request_id = "loop-scenario" if len(messages) == 1 else f"loop-scenario-{nth}"
        ctx = replace(ctx, request_id=request_id)
        async for event in run(
            message,
            ctx,
            provider=provider,
            history=history,
            inspectors=[AllowInspector()],
            tool_executor=executor,
            hook_manager=manager,
        ):
            events.append(event)
    return events


def _turn_context(
    cfg: RuntimeConfig,
    *,
    workspace: Path | None,
    read_ledger: FileReadLedger | None,
    hook_memory: MemorySubsystem | None,
    request_id: str,
) -> TurnContext:
    return TurnContext(
        thread_id="loop-scenario",
        request_id=request_id,
        agent_md="# Scripted scenario\n",
        memory_index=[],
        skills=[],
        tools=_core_tool_defs(include_task_tool=True),
        user_id=None,
        parent_run_id=None,
        model=cfg.model.name or "fake",
        summarization_model=cfg.model.summarization_model,
        context_window_tokens=context_window_tokens(cfg),
        workspace_root=workspace,
        memory=hook_memory,
        config=cfg,
        read_ledger=read_ledger,
    )


def _view(
    scenario: Scenario,
    *,
    events: list[object],
    workspace: Path | None,
    pristine: Path | None,
    provider: ScriptedProvider,
    history: FakeHistory,
    record: EvalRecord,
) -> RunView:
    script_len = len(scenario.script or [])
    return RunView(
        events=events,
        workspace=workspace,
        fixture_dir=pristine,
        memory_text="\n".join(record.memory_injected_lines),
        provider_calls=provider.stream_calls,
        provider_requests=_provider_requests(provider),
        history=list(history.rows),
        scripted=True,
        script_remaining=max(0, script_len - provider.stream_calls),
        script_partial=bool(scenario.script_partial),
        driver="loop",
    )


def _provider_requests(provider: ScriptedProvider) -> list[ProviderRequest]:
    requests: list[ProviderRequest] = []
    pairs = zip(provider.stream_messages, provider.stream_tools, strict=True)
    for messages, tools in pairs:
        system: list[str] = []
        body: list[str] = []
        for message in messages:
            text = _message_text(message)
            if message.role == "system":
                system.append(text)
            else:
                body.append(text)
        requests.append(
            ProviderRequest(
                tools=[tool.name for tool in tools],
                system="\n".join(part for part in system if part),
                messages=body,
            )
        )
    return requests


def _message_text(message: object) -> str:
    content = getattr(message, "content", ())
    parts = [block.text for block in content if isinstance(block, Text)]
    return "".join(parts)


def _check(scenario: Scenario, view: RunView) -> None:
    problems = check_invariants(view, scenario.invariants_skip)
    problems.extend(evaluate(scenario.assertions, view))
    if problems:
        detail = "\n".join(problems)
        raise AssertionError(f"{scenario.case}\n{detail}")


async def _shutdown(
    executor: RecordingExecutor | _ObservedExecutor,
    hook_memory: MemorySubsystem | None,
) -> None:
    closer = getattr(executor, "aclose", None)
    if callable(closer):
        await closer()
    if hook_memory is not None:
        await hook_memory.close()


@contextmanager
def _pushed_env(updates: Mapping[str, str]) -> Iterator[None]:
    """Apply ``config.env`` for the run, then put the process environment back.

    Called only after :func:`build_runtime_config` has captured pins, so these
    keys are not frozen into the process-wide pin table.
    """
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
