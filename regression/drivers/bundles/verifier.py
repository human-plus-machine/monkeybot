"""Verifier ledger, tracker, judge, and nudge."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass

from monkeybot.core.config.snapshot import RuntimeConfig
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.persistence.branches import ROOT_BRANCH_ID, BranchRecord, branch_thread_id
from monkeybot.core.persistence.runs import make_run_id
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.verifier.actuator import NudgeActuator
from monkeybot.core.verifier.binding import resolve_session_verifier_model
from monkeybot.core.verifier.judge import JudgeWorker, ProviderJudge
from monkeybot.core.verifier.ledger import GoalLedger
from monkeybot.core.verifier.mailbox import VerdictMailbox
from monkeybot.core.verifier.rewrite_effects import VerifierRewriteEffects
from monkeybot.core.verifier.tracker import ProgressTracker
from regression.drivers.bundles.protocol import Bundle, LoopDriverError, RunCtx
from regression.schema.assertions import StoreView
from regression.schema.scenario import Scenario
from testkit.verifier import ScriptedClassifier, ScriptedJudge


@dataclass
class _VerifierRuntime:
    """Ledger, mailbox, and judge installed for one scenario."""

    ledger: GoalLedger | None = None
    mailbox: VerdictMailbox | None = None
    tracker: ProgressTracker | None = None
    judge: JudgeWorker | None = None
    scripted_judge: ScriptedJudge | None = None


def _ledger_store_needed(hook_names: Sequence[str], cfg: RuntimeConfig) -> bool:
    """True when the gateway would open a goal-ledger store for this scenario."""
    return "verifier" in hook_names and cfg.verifier.enabled and cfg.verifier.ledger.enabled


def _register_goal_ledger(manager: HookManager, ledger: GoalLedger) -> None:
    async def _on_user_message(payload: HookPayload) -> None:
        await ledger.on_user_message(payload)
        try:
            await ledger.wait_idle(payload.thread_id)
        except TimeoutError as exc:
            raise LoopDriverError(
                "goal ledger classifier did not finish before the next provider call"
            ) from exc

    manager.register(HookEvent.USER_MESSAGE, _on_user_message)


def _install_verifier(
    manager: HookManager,
    cfg: RuntimeConfig,
    env: Mapping[str, str],
    backend: SQLiteStorageBackend | None,
    hook_names: Sequence[str],
) -> _VerifierRuntime | None:
    """Register the gateway verifier hooks.

    ``verifier.enabled`` off registers nothing, the same as the gateway.
    A set ``MONKEYBOT_CLASSIFIER_ROWS`` builds ``ScriptedClassifier``. A set
    ``MONKEYBOT_JUDGE_SCRIPT`` builds ``ScriptedJudge`` for ``ProviderJudge``.
    The user-message hook waits until that classification is stored: the
    production hook only enqueues, and the loop settles ``USER_MESSAGE``
    before the first provider call.
    """
    if "verifier" not in hook_names or not cfg.verifier.enabled:
        return None
    runtime = _VerifierRuntime()
    if cfg.verifier.ledger.enabled and backend is not None:
        store = backend.goal_ledger()
        if store is not None:
            classifier = ScriptedClassifier.from_env(env) or ScriptedClassifier([])
            runtime.ledger = GoalLedger(
                store,
                classifier,
                max_entries_per_thread=cfg.verifier.ledger.max_entries_per_thread,
            )
            _register_goal_ledger(manager, runtime.ledger)
    if not cfg.verifier.tracker.enabled:
        return runtime
    runtime.mailbox = VerdictMailbox()
    judge: JudgeWorker | None = None
    if cfg.verifier.judge.enabled:
        scripted = ScriptedJudge.from_env(env)
        runtime.scripted_judge = scripted

        def _judge_provider() -> ScriptedJudge | None:
            return scripted

        def _judge_model(session: str = "") -> str:
            return resolve_session_verifier_model(
                cfg,
                cfg.verifier.judge.model,
                session_model=session,
            )

        judge = JudgeWorker(
            runtime.mailbox,
            ProviderJudge(_judge_provider, model=_judge_model),
            ledger_fn=lambda: runtime.ledger,
            config=cfg.verifier.judge,
            max_severity=cfg.verifier.escalation.max_severity,
        )
        runtime.judge = judge
    runtime.tracker = ProgressTracker(
        runtime.mailbox,
        ledger_fn=lambda: runtime.ledger,
        config=cfg.verifier.tracker,
        judge=judge,
    )
    runtime.tracker.register(manager)
    NudgeActuator(runtime.mailbox).register(manager)
    return runtime


def _store_spec(scenario: Scenario) -> Mapping[str, object]:
    raw = scenario.assertions
    if not isinstance(raw, Mapping):
        return {}
    store = raw.get("store")
    return store if isinstance(store, Mapping) else {}


async def verifier_store_snapshot(ctx: RunCtx, thread_id: str) -> StoreView | None:
    """Count verifier ledger rows and conversation branches for ``store`` assertions.

    A scenario that asks for branches is the history-rewrite case: copy the
    ledger onto a new branch, then drop those copied rows. The source thread's
    goal remains.
    """
    ledger = ctx.goal_ledger
    backend = ctx.backend
    if ledger is None or backend is None:
        return None
    store = backend.goal_ledger()
    if store is None:
        return None
    with suppress(TimeoutError):
        await ledger.wait_idle(thread_id)
    if "branches" in _store_spec(ctx.scenario):
        await _copy_goal_onto_branch(ctx, thread_id)
    entries = await store.list_entries(thread_id)
    branch_rows = await backend.branches().list(thread_id)
    return StoreView(goals=len(entries), branches=len(branch_rows))


async def _copy_goal_onto_branch(ctx: RunCtx, thread_id: str) -> None:
    ledger = ctx.goal_ledger
    backend = ctx.backend
    if ledger is None or backend is None:
        return
    branches = backend.branches()
    if await branches.list(thread_id):
        return
    store = backend.goal_ledger()
    if store is None:
        return
    entries = await store.list_entries(thread_id)
    branch_id = make_run_id()
    target = branch_thread_id(thread_id, branch_id)
    now = int(time.time() * 1000)
    await branches.create(
        BranchRecord(
            branch_id=branch_id,
            session_id=thread_id,
            thread_id=target,
            parent_branch_id=ROOT_BRANCH_ID,
            fork_row_id=None,
            op="edit",
            created_at=now,
            last_active_at=now,
            is_active=True,
        )
    )
    tracker = None if ctx.verifier is None else ctx.verifier.tracker
    effects = VerifierRewriteEffects(ledger=ledger, tracker=tracker)
    await effects.branched(thread_id, target, ())
    dropped = {entry.source_row_id for entry in entries if entry.source_row_id}
    if dropped:
        await effects.truncated(target, dropped)


async def _close_ledger(ledger: GoalLedger) -> None:
    """Stop one ledger's workers before the temporary database closes."""
    with suppress(TimeoutError):
        await ledger.wait_all_idle()
    workers = list(ledger._workers.values())
    ledger.close()
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)


async def _close_verifier(runtime: _VerifierRuntime | None) -> None:
    """Stop judge and ledger workers before the temporary database closes."""
    if runtime is None:
        return
    if runtime.judge is not None:
        await runtime.judge.aclose()
    if runtime.ledger is not None:
        await _close_ledger(runtime.ledger)


class _VerifierBundle(Bundle):
    order = 70
    hooks = ("verifier",)
    after_eval = True

    async def prepare(self, ctx: RunCtx) -> None:
        if ctx.backend is None and _ledger_store_needed(ctx.hook_names, ctx.cfg):
            ctx.backend = SQLiteStorageBackend(f"sqlite:///{ctx.workdir / 'loops.db'}")
            await ctx.backend.open()

    def register(self, manager: HookManager, ctx: RunCtx) -> None:
        ctx.verifier = _install_verifier(manager, ctx.cfg, ctx.env, ctx.backend, ctx.hook_names)
        ctx.goal_ledger = None if ctx.verifier is None else ctx.verifier.ledger

    async def close(self, ctx: RunCtx) -> None:
        await _close_verifier(ctx.verifier)
        ctx.verifier = None


BUNDLE = _VerifierBundle()
