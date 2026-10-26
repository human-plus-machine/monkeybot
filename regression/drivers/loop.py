"""Drive :func:`monkeybot.core.runtime.loop.run` for one scripted scenario.

Bundles under :mod:`regression.drivers.bundles` own setup. This module is the
turn loop. ``EvalHook`` registers here, after the bundles that inject memory
lines and before the verifier, so it still sees those lines. A new hook is a
new bundle module. It does not edit this file.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from monkeybot.core.config.snapshot import context_window_tokens
from monkeybot.core.context import (
    GOAL_TOOL_NAMES,
    PendingResponseBusPort,
    TurnContext,
    _core_tool_defs,
)
from monkeybot.core.hooks import HookManager
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.runtime.events import Error, TurnComplete, event_from_json
from monkeybot.core.runtime.input_admission import InputAdmission
from monkeybot.core.runtime.loop import run
from monkeybot.core.tools.permission import SessionApprovals
from monkeybot.core.tools.read_ledger import FileReadLedger
from monkeybot.core.verifier.ledger import GoalLedger
from monkeybot.core.verifier.mailbox import VerdictMailbox
from monkeybot.todo_list import TodoListReminder, TodoListStore
from regression.drivers.bundles import discover, registered_hooks
from regression.drivers.bundles.hooks import _installed_secret_scanner
from regression.drivers.bundles.policy import _grants_persist, _inspectors, _PolicyBundle
from regression.drivers.bundles.prompt_context import apply_project_context, message_skills
from regression.drivers.bundles.protocol import LoopDriverError, RunCtx
from regression.drivers.bundles.session import (
    _apply_user_slash,
    _store_snapshot,
)
from regression.drivers.bundles.store import apply_turn_usage
from regression.drivers.bundles.subagents import (
    _HeldCancel,
    _subagent_run_dir,
    _SubagentPublisher,
    _task_cancel_step,
)
from regression.drivers.bundles.verifier import verifier_store_snapshot
from regression.drivers.bundles.workspace import resolve_workspace
from regression.drivers.record import EvalHook, EvalRecord
from regression.drivers.support.config import _LiveConfig, _scenario_config, _with_gate_enabled
from regression.drivers.support.env import _expand_scenario_env, _pushed_env
from regression.drivers.support.executor import _ObservedExecutor
from regression.drivers.support.paths import _SESSION_ID
from regression.drivers.support.tokens import (
    _JudgeScriptProvider,
    _message_text,
    _ResultTokenProvider,
    _script_turns,
    _session_groups,
    _user_messages,
)
from regression.schema.assertions import ProviderRequest, RunView, StoreView, evaluate
from regression.schema.invariants import check_invariants
from regression.schema.scenario import Scenario
from regression.schema.steps import (
    AnswerAskUserStep,
    CancelStep,
    ConfirmToolStep,
    PromoteFollowUpStep,
    QueueFollowUpStep,
    SetMemoryLinesStep,
    SteerStep,
    StepBindings,
    StepSchedule,
    loop_driver_rejection,
)
from testkit.clock import FakeClock
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory
from testkit.pending_bus import PresetPendingBus
from testkit.provider import ScriptedProvider

_HISTORICAL_HOOKS = (
    "verification_gate",
    "memory",
    "verifier",
    "git_context",
    "read_ledger_compaction",
    "evidence_guard",
    "secret_scanner",
)


class _SessionSlice:
    """One session's events and history, split so each has its own turn end."""

    def __init__(self, events: list[object], history: list[Any]) -> None:
        self.events = events
        self.history = history


@dataclass
class LoopRun:
    """Events the loop yielded, and the tool calls the executor recorded."""

    events: list[object]
    calls: list[ToolCall]
    view: RunView
    clock: FakeClock


def _hook_names(scenario: Scenario) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for name in scenario.hooks:
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _registered_sentence() -> str:
    names = [name for bundle in discover() for name in bundle.hooks]
    ordered = _HISTORICAL_HOOKS if set(names) == set(_HISTORICAL_HOOKS) else tuple(names)
    if len(ordered) < 2:
        return ", ".join(ordered)
    return ", ".join(ordered[:-1]) + ", and " + ordered[-1]


def _reject_unsupported(scenario: Scenario) -> None:
    if scenario.mode != "scripted":
        raise LoopDriverError(f"loop driver runs scripted scenarios, not mode {scenario.mode!r}")
    if scenario.driver not in (None, "loop"):
        raise LoopDriverError(f"loop driver cannot run driver {scenario.driver!r}")
    if scenario.sessions is not None:
        if not scenario.sessions or any(not group for group in scenario.sessions):
            raise LoopDriverError("scripted loop scenario sessions need at least one message each")
    elif not scenario.messages:
        raise LoopDriverError("scripted loop scenario needs at least one message")
    if scenario.session_pause_sec:
        raise LoopDriverError(
            "loop driver does not pause between sessions; settlement waits are explicit"
        )
    if not scenario.script:
        raise LoopDriverError("scripted loop scenario is missing script")
    for step in scenario.steps:
        reason = loop_driver_rejection(step.action)
        if reason is not None:
            raise LoopDriverError(reason)
    allowed = registered_hooks()
    missing = [name for name in _hook_names(scenario) if name not in allowed]
    if missing:
        joined = ", ".join(missing)
        raise LoopDriverError(
            f"loop driver does not register hook bundle(s): {joined}. "
            f"Registered bundles are {_registered_sentence()}"
        )
    drawers = scenario.memory.palace if scenario.memory is not None else []
    if drawers and "memory" not in scenario.hooks:
        raise LoopDriverError(
            "memory.palace is set but hooks does not include memory; "
            "the loop driver will not drop the palace seed"
        )


async def run_scenario(scenario: Scenario) -> LoopRun:
    """Run ``scenario`` through the real loop, then invariants and assertions.

    The temporary workspace is removed when this function returns. Checks run
    while it still exists. A failed invariant or assertion raises
    :class:`AssertionError`.
    """
    _reject_unsupported(scenario)
    with tempfile.TemporaryDirectory(prefix="mb-loop-") as raw:
        return await _run(scenario, Path(raw))


async def _run(scenario: Scenario, workdir: Path) -> LoopRun:
    cfg, env, merged, config_path = _scenario_config(scenario, workdir)
    hook_names = _hook_names(scenario)
    if "verification_gate" in hook_names:
        cfg = _with_gate_enabled(cfg)
    ctx = RunCtx(
        scenario=scenario,
        workdir=workdir,
        cfg=cfg,
        env=env,
        merged=merged,
        config_path=config_path,
        hook_names=hook_names,
        gate="verification_gate" in hook_names,
    )
    try:
        for bundle in discover():
            await bundle.prepare(ctx)
        if ctx.boot_error:
            return _boot_failure(scenario, ctx)
        ctx.messages = _user_messages(scenario, ctx.workspace, ctx.tokens)
        ctx.record = EvalRecord()
        manager = HookManager()
        for bundle in discover():
            if not bundle.after_eval:
                bundle.register(manager, ctx)
        # Last among the PRE_TURN injectors, so it sees the lines they added.
        EvalHook(ctx.record).register(manager)
        if ctx.todo_store is not None:
            TodoListReminder().register(manager)
        for bundle in discover():
            if bundle.after_eval:
                bundle.register(manager, ctx)
        turns = _script_turns(scenario, ctx.tokens)
        provider_name = _scripted_provider_name(scenario)
        scripted_judge = None if ctx.verifier is None else ctx.verifier.scripted_judge
        if scripted_judge is None:
            provider: ScriptedProvider = _ResultTokenProvider(turns, name=provider_name)
        else:
            # ProviderJudge prefers the bound session provider. Route only the
            # judge prompt to ScriptedJudge so the agent script is not consumed.
            provider = _JudgeScriptProvider(turns, scripted_judge, name=provider_name)
        history = FakeHistory(list(scenario.history))
        clock = FakeClock()
        _expand_scenario_env(ctx.env, ctx.workspace)
        groups = _session_groups(scenario, ctx.messages, ctx.workspace, ctx.tokens)
        with _pushed_env(ctx.env), _installed_secret_scanner(ctx.scanner_mode):
            events, slices = await _drive(
                groups,
                scenario=scenario,
                provider=provider,
                history=history,
                executor=ctx.executor,
                manager=manager,
                live=_LiveConfig(
                    cfg=ctx.cfg,
                    env=ctx.env,
                    merged=ctx.merged,
                    path=ctx.config_path,
                    workdir=workdir,
                    gate=ctx.gate,
                    mcp=ctx.mcp_client,
                    mcp_path=ctx.mcp_path,
                    tools=ctx.advertised,
                    catalog=ctx.catalog,
                ),
                workspace=ctx.workspace,
                read_ledger=ctx.read_ledger,
                hook_memory=ctx.hook_memory,
                clock=clock,
                todo_store=ctx.todo_store,
                loops_available=ctx.loop_store is not None,
                extra_tool_defs=ctx.extra_tool_defs,
                goal_ledger=ctx.goal_ledger,
                verdict_mailbox=None if ctx.verifier is None else ctx.verifier.mailbox,
                policy=ctx.policy,
                grants_persist=_grants_persist(ctx.policy),
                approvals_persist=ctx.approvals_persist,
                project_instructions=ctx.project_instructions,
                repo_overview=ctx.repo_overview,
                session_id=ctx.session_id,
                attachment_store=ctx.attachment_store,
                computer_inspector=ctx.computer_inspector,
            )
            view = _view(
                scenario,
                events=events,
                workspace=ctx.workspace,
                pristine=ctx.pristine,
                provider=provider,
                history=history,
                record=ctx.record,
                subagent_dir=_subagent_run_dir(ctx.workspace),
            )
            if slices is not None:
                view = replace(view, history=list(slices[-1].history))
            if ctx.loop_store is not None:
                view.store = await _store_snapshot(ctx.loop_store)
            elif ctx.goal_ledger is not None:
                view.store = await verifier_store_snapshot(ctx, ctx.session_id)
            if ctx.outbox_count is not None:
                if view.store is None:
                    view.store = StoreView(outbox=ctx.outbox_count)
                else:
                    view.store.outbox = ctx.outbox_count
            await apply_turn_usage(ctx, events, view)
            if slices is None:
                _check(scenario, view)
            else:
                _check_sessions(scenario, view, slices)
            return LoopRun(events=events, calls=list(ctx.executor.calls), view=view, clock=clock)
    finally:
        for bundle in reversed(discover()):
            await bundle.close(ctx)


async def _drive(
    groups: list[list[str]],
    *,
    scenario: Scenario,
    provider: ScriptedProvider,
    history: FakeHistory,
    executor: RecordingExecutor | _ObservedExecutor,
    manager: HookManager,
    live: _LiveConfig,
    workspace: Path | None,
    read_ledger: FileReadLedger | None,
    hook_memory: Any,
    clock: FakeClock,
    todo_store: TodoListStore | None = None,
    loops_available: bool = False,
    extra_tool_defs: Any = (),
    goal_ledger: GoalLedger | None = None,
    verdict_mailbox: VerdictMailbox | None = None,
    policy: _PolicyBundle | None = None,
    grants_persist: Callable[[str, str], bool] | None = None,
    approvals_persist: Callable[[str, str], bool] | None = None,
    project_instructions: str = "",
    repo_overview: str = "",
    session_id: str = _SESSION_ID,
    attachment_store: Any = None,
    computer_inspector: Any = None,
) -> tuple[list[object], list[_SessionSlice] | None]:
    events: list[object] = []
    needs_bus = any(
        isinstance(step, (AnswerAskUserStep, ConfirmToolStep)) for step in scenario.steps
    )
    needs_admission = any(
        isinstance(step, (SteerStep, QueueFollowUpStep, PromoteFollowUpStep))
        for step in scenario.steps
    )
    cancelled: asyncio.Event | None
    if _task_cancel_step(scenario):
        held = _HeldCancel()
        held.hold = True
        cancelled = held
    elif any(isinstance(step, CancelStep) for step in scenario.steps):
        cancelled = asyncio.Event()
    else:
        cancelled = None
    admission = InputAdmission()
    bus = PresetPendingBus() if needs_bus else None
    if bus is not None:
        # The gateway session bus remembers an "always" approval. LoopStartInspector
        # does not consult that set; PermissionInspector does.
        bus.session_approvals = SessionApprovals()
    memory_lines: list[str] | None = None
    if hook_memory is not None and any(
        isinstance(step, SetMemoryLinesStep) for step in scenario.steps
    ):
        memory_lines = []
        original_load = hook_memory.load_index

        async def _load_index(thread_id: str | None = None) -> list[str]:
            base = await original_load(thread_id=thread_id)
            return [*base, *(memory_lines or [])]

        hook_memory.load_index = _load_index
    bindings = StepBindings(
        clock=clock,
        admission=admission,
        cancelled=cancelled,
        bus=bus,
        workspace=workspace,
        request_id="loop-scenario",
        reload=live.reload,
        history=history,
        thread_id=session_id,
        memory_lines=memory_lines,
    )
    schedule = StepSchedule(list(scenario.steps), bindings)
    publisher = _SubagentPublisher(
        events,
        schedule,
        cancelled if isinstance(cancelled, _HeldCancel) else None,
        workspace,
    )

    def on_publish(raw: str) -> None:
        event = event_from_json(raw)
        events.extend(schedule.observe(event))
        events.append(event)

    if bus is not None:
        bus.on_publish = on_publish
    turn = _turn_context(
        live.cfg,
        workspace=workspace,
        read_ledger=read_ledger,
        hook_memory=hook_memory,
        request_id="loop-scenario",
        cancelled=cancelled,
        sse_bus=bus,
        tools=live.tools,
        catalog=live.catalog,
        todo_store=todo_store,
        loops_available=loops_available,
        extra_tool_defs=extra_tool_defs,
        goal_ledger=goal_ledger,
        verdict_mailbox=verdict_mailbox,
        event_publisher=publisher,
        grants_persist=grants_persist,
        approvals_persist=approvals_persist,
    )
    turn = apply_project_context(
        turn,
        project_instructions=project_instructions,
        repo_overview=repo_overview,
    )
    inspectors = _inspectors(scenario, verdict_mailbox, policy, computer_inspector)
    multi = len(groups) > 1
    split_turns = sum(len(group) for group in groups) > 1
    slices: list[_SessionSlice] = []
    for session_index, session_messages in enumerate(groups, start=1):
        active_history = history if session_index == 1 else FakeHistory()
        thread_id = session_id if not multi else f"{session_id}-{session_index}"
        bindings.history = active_history
        bindings.thread_id = thread_id
        turn = replace(turn, thread_id=thread_id)
        for nth, message in enumerate(session_messages, start=1):
            start = len(events)
            if multi:
                request_id = (
                    f"loop-scenario-{session_index}"
                    if len(session_messages) == 1
                    else f"loop-scenario-{session_index}-{nth}"
                )
            else:
                request_id = (
                    "loop-scenario" if len(session_messages) == 1 else f"loop-scenario-{nth}"
                )
            bindings.request_id = request_id
            discovered = message_skills(executor, live.workdir, live.merged, live.env)
            turn = replace(
                turn,
                request_id=request_id,
                config=live.cfg,
                context_window_tokens=context_window_tokens(live.cfg),
                model=live.cfg.model.name or turn.model,
                skills=discovered,
                invoked_skill=None,
                tools=[tool for tool in turn.tools if tool.name not in GOAL_TOOL_NAMES],
            )
            turn = _apply_user_slash(turn, message)
            live.turn = turn
            async for event in run(
                message,
                turn,
                provider=provider,
                history=active_history,
                inspectors=inspectors,
                tool_executor=executor,
                hook_manager=manager,
                cancelled=cancelled,
                attachment_store=attachment_store,
                input_admission=admission if needs_admission else None,
            ):
                events.extend(schedule.observe(event))
                events.append(event)
                if live.mcp_pending:
                    await live.refresh_mcp()
            if split_turns:
                slices.append(
                    _SessionSlice(events=list(events[start:]), history=list(active_history.rows))
                )
    if publisher.watch is not None:
        await publisher.watch
    leftover = schedule.pending()
    if leftover:
        summary = ", ".join(
            f"{step.action} when {step.when.event} nth {step.when.nth}" for step in leftover
        )
        raise LoopDriverError(f"steps did not fire: {summary}")
    return events, slices or None


def _turn_context(
    cfg: Any,
    *,
    workspace: Path | None,
    read_ledger: FileReadLedger | None,
    hook_memory: Any,
    request_id: str,
    cancelled: asyncio.Event | None = None,
    sse_bus: PendingResponseBusPort | None = None,
    tools: list[Any] | None = None,
    catalog: tuple[str, ...] = (),
    todo_store: TodoListStore | None = None,
    loops_available: bool = False,
    extra_tool_defs: Any = (),
    goal_ledger: GoalLedger | None = None,
    verdict_mailbox: VerdictMailbox | None = None,
    event_publisher: _SubagentPublisher | None = None,
    grants_persist: Callable[[str, str], bool] | None = None,
    approvals_persist: Callable[[str, str], bool] | None = None,
) -> TurnContext:
    advertised = list(tools) if tools is not None else _core_tool_defs(include_task_tool=True)
    names = {tool.name for tool in advertised}
    for extra in extra_tool_defs:
        if extra.name not in names:
            advertised.append(extra)
            names.add(extra.name)
    return TurnContext(
        thread_id=_SESSION_ID,
        request_id=request_id,
        agent_md="# Scripted scenario\n",
        memory_index=[],
        skills=[],
        tools=advertised,
        catalog_mcp_servers=catalog,
        scheduled_loops_available=loops_available,
        user_id=None,
        parent_run_id=None,
        model=cfg.model.name or "fake",
        summarization_model=cfg.model.summarization_model,
        cancelled=cancelled,
        context_window_tokens=context_window_tokens(cfg),
        workspace_root=workspace,
        memory=hook_memory,
        sse_bus=sse_bus,
        event_publisher=event_publisher,
        config=cfg,
        read_ledger=read_ledger,
        todo_store=todo_store,
        goal_ledger=goal_ledger,
        verdict_mailbox=verdict_mailbox,
        grants_persist=grants_persist,
        approvals_persist=approvals_persist,
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
    subagent_dir: Path | None = None,
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
        subagent_dir=subagent_dir,
        scripted=True,
        script_remaining=max(0, script_len - provider.stream_calls),
        script_partial=bool(scenario.script_partial),
        driver="loop",
    )


def _scripted_provider_name(scenario: Scenario) -> str:
    """Name the scripted provider reports. Default ``fake`` is outside the budget set."""
    if scenario.config is None:
        return "fake"
    raw = scenario.config.model_dump().get("scripted_provider")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return "fake"


def _provider_requests(provider: ScriptedProvider) -> list[ProviderRequest]:
    requests: list[ProviderRequest] = []
    recorded = getattr(provider, "stream_thinking_budgets", None)
    count = len(provider.stream_messages)
    budgets = recorded if isinstance(recorded, list) and len(recorded) == count else [None] * count
    recorded_models = provider.stream_models
    models = recorded_models if len(recorded_models) == count else [""] * count
    recorded_search = getattr(provider, "stream_vertex_google_search", None)
    searches = (
        recorded_search
        if isinstance(recorded_search, list) and len(recorded_search) == count
        else [False] * count
    )
    pairs = zip(
        provider.stream_messages,
        provider.stream_tools,
        budgets,
        models,
        searches,
        strict=True,
    )
    for messages, tools, budget, model, search in pairs:
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
                thinking_budget=budget if isinstance(budget, int) else None,
                model=model if isinstance(model, str) else "",
                vertex_google_search=bool(search),
            )
        )
    return requests


def _check(scenario: Scenario, view: RunView) -> None:
    problems = check_invariants(view, scenario.invariants_skip)
    problems.extend(evaluate(scenario.assertions, view))
    if problems:
        detail = "\n".join(problems)
        raise AssertionError(f"{scenario.case}\n{detail}")


def _boot_failure(scenario: Scenario, ctx: RunCtx) -> LoopRun:
    """Surface a memory boot error without starting the turn."""
    clock = FakeClock()
    provider = _ResultTokenProvider(
        _script_turns(scenario, ctx.tokens),
        name=_scripted_provider_name(scenario),
    )
    history = FakeHistory(list(scenario.history))
    events: list[object] = [
        Error(request_id=_SESSION_ID, error=ctx.boot_error or ""),
        TurnComplete(request_id=_SESSION_ID),
    ]
    view = _view(
        scenario,
        events=events,
        workspace=ctx.workspace,
        pristine=ctx.pristine,
        provider=provider,
        history=history,
        record=EvalRecord(),
        subagent_dir=_subagent_run_dir(ctx.workspace),
    )
    _check(scenario, view)
    return LoopRun(events=events, calls=[], view=view, clock=clock)


def _check_sessions(scenario: Scenario, combined: RunView, slices: list[_SessionSlice]) -> None:
    """Invariant-check each user message. Assertions see the combined run.

    Each message is one ``run()`` and one ``TurnComplete``. More than one
    message is sliced per message so that invariant still holds.
    ``strict_script`` applies to the last slice only.
    """
    problems: list[str] = []
    last = len(slices) - 1
    for index, sliver in enumerate(slices):
        view = replace(
            combined,
            events=sliver.events,
            history=sliver.history,
            scripted=index == last,
            script_remaining=combined.script_remaining if index == last else 0,
        )
        for problem in check_invariants(view, scenario.invariants_skip):
            problems.append(f"turn {index + 1}: {problem}")
    problems.extend(evaluate(scenario.assertions, combined))
    if problems:
        detail = "\n".join(problems)
        raise AssertionError(f"{scenario.case}\n{detail}")


__all__ = ["LoopDriverError", "LoopRun", "resolve_workspace", "run_scenario"]
