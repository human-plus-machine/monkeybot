"""Goal ledger: enqueue-only hooks, per-thread classifier worker, ResolvedIntent cache."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, replace

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.goal_ledger import (
    Channel,
    Classification,
    Constraint,
    ConstraintKind,
    GoalEntry,
    GoalLedgerStore,
    Intent,
    Provenance,
    ResolvedIntent,
    Status,
    empty_resolved,
    new_entry_id,
    now_ms,
    resolve_intent,
)
from monkeybot.core.verifier.classify import ClassifierPort, fail_open_classification

logger = logging.getLogger(__name__)

_THREAD_STATE_CAP = 256
_RECORD_INTENTS = (Intent.CORRECTION, Intent.ANSWER, Intent.NOISE)
_TERMINAL_STATUSES = (Status.SATISFIED, Status.SUPERSEDED, Status.ABANDONED)


@dataclass(frozen=True)
class _Job:
    thread_id: str
    verbatim: str
    provenance: Provenance
    channel: Channel | None


class GoalLedger:
    """Authoritative intent store. Hooks only enqueue; classification is owned here."""

    def __init__(
        self,
        store: GoalLedgerStore,
        classifier: ClassifierPort,
        *,
        max_entries_per_thread: int = 64,
        thread_cap: int = _THREAD_STATE_CAP,
    ) -> None:
        self._store = store
        self._classifier = classifier
        self._max_entries = max(1, max_entries_per_thread)
        self._thread_cap = max(1, thread_cap)
        self._queues: dict[str, asyncio.Queue[_Job]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._pending: dict[str, int] = {}
        self._cache: OrderedDict[str, ResolvedIntent] = OrderedDict()
        self._closed = False

    def register(self, manager: HookManager) -> None:
        manager.register(HookEvent.USER_MESSAGE, self.on_user_message)

    async def on_user_message(self, payload: HookPayload) -> None:
        text = (payload.user_message or "").strip()
        if not text:
            return
        self.admit(
            payload.thread_id,
            text,
            provenance=Provenance.HUMAN,
            channel=Channel.MESSAGE,
        )

    def admit(
        self,
        thread_id: str,
        verbatim: str,
        *,
        provenance: Provenance,
        channel: Channel | None,
    ) -> None:
        """Enqueue a ledger write. Returns without waiting on the classifier."""
        if self._closed or not verbatim.strip():
            return
        queue = self._ensure_worker(thread_id)
        self._pending[thread_id] = self._pending.get(thread_id, 0) + 1
        self._touch_pending_view(thread_id)
        queue.put_nowait(
            _Job(
                thread_id=thread_id,
                verbatim=verbatim.strip(),
                provenance=provenance,
                channel=channel,
            )
        )

    def resolved_intent(self, thread_id: str) -> ResolvedIntent | None:
        view = self._cache.get(thread_id)
        pending = self._pending.get(thread_id, 0) > 0
        if view is None:
            return empty_resolved(pending=pending) if pending else None
        if pending and not view.pending_classification:
            return replace(view, pending_classification=True)
        self._cache.move_to_end(thread_id)
        return view

    def compaction_facts(self, thread_id: str) -> str | None:
        view = self.resolved_intent(thread_id)
        if view is None:
            return None
        if view.pending_classification and view.active_goal is None:
            return None
        return format_compaction_facts(view)

    def subagent_context(self, thread_id: str) -> str | None:
        view = self.resolved_intent(thread_id)
        if view is None or (view.active_goal is None and not view.standing_constraints):
            return None
        return format_subagent_context(view)

    async def wait_idle(self, thread_id: str, *, timeout_s: float = 5.0) -> None:
        queue = self._queues.get(thread_id)
        if queue is None:
            return
        await asyncio.wait_for(queue.join(), timeout=timeout_s)

    def close(self) -> None:
        self._closed = True
        for task in self._workers.values():
            task.cancel()
        self._workers.clear()
        self._queues.clear()
        self._pending.clear()

    def _ensure_worker(self, thread_id: str) -> asyncio.Queue[_Job]:
        existing = self._queues.get(thread_id)
        if existing is not None:
            return existing
        queue: asyncio.Queue[_Job] = asyncio.Queue()
        worker = self._run_worker(thread_id, queue)
        try:
            task = asyncio.create_task(
                worker,
                name=f"goal-ledger-{thread_id[:12]}",
            )
        except Exception:
            worker.close()
            raise
        self._queues[thread_id] = queue
        self._workers[thread_id] = task
        return queue

    async def _run_worker(self, thread_id: str, queue: asyncio.Queue[_Job]) -> None:
        while True:
            job = await queue.get()
            try:
                if self._closed:
                    return
                await self._handle_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "goal_ledger worker failed %s",
                    kv(thread_id=thread_id),
                    exc_info=True,
                )
            finally:
                queue.task_done()
                pending = self._pending.get(thread_id, 0) - 1
                if pending <= 0:
                    self._pending.pop(thread_id, None)
                else:
                    self._pending[thread_id] = pending
                if not self._closed:
                    await self._refresh_view(thread_id)

    async def _handle_job(self, job: _Job) -> None:
        if job.provenance != Provenance.HUMAN:
            await self._persist_non_human(job)
            return
        entries = await self._store.list_entries(job.thread_id)
        open_entries = [
            e for e in entries if e.provenance == Provenance.HUMAN and e.status == Status.ACTIVE
        ]
        try:
            result = await self._classifier.classify(job.verbatim, open_entries)
        except Exception:
            logger.warning(
                "goal_ledger classify raised %s",
                kv(thread_id=job.thread_id),
                exc_info=True,
            )
            result = fail_open_classification(open_entries)
        await self._apply_classification(job, result, entries)

    async def _persist_non_human(self, job: _Job) -> None:
        entry = GoalEntry(
            entry_id=new_entry_id(),
            thread_id=job.thread_id,
            seq=0,
            verbatim=job.verbatim,
            provenance=job.provenance,
            channel=job.channel,
            intent=Intent.NOISE,
            status=Status.SATISFIED,
            relates_to=None,
            constraints=(),
            done_when=(),
            created_at_ms=now_ms(),
        )
        await self._store.commit_classified(entry)

    async def _apply_classification(
        self,
        job: _Job,
        result: Classification,
        entries: list[GoalEntry],
    ) -> None:
        entry_id = new_entry_id()
        constraints = tuple(
            Constraint(
                kind=draft.kind,
                pattern=draft.pattern,
                source_entry_id=entry_id,
                verbatim=draft.verbatim,
            )
            for draft in result.constraints
        )
        relates_to = result.relates_to
        if relates_to and not any(e.entry_id == relates_to for e in entries):
            relates_to = None
        if relates_to is None and result.intent in (
            Intent.REFINEMENT,
            Intent.SCOPE_CHANGE,
            Intent.PREEMPT,
            Intent.CORRECTION,
        ):
            active = next(
                (
                    e
                    for e in reversed(entries)
                    if e.provenance == Provenance.HUMAN and e.status == Status.ACTIVE
                ),
                None,
            )
            if active is not None:
                relates_to = active.entry_id
        status_updates: list[tuple[str, Status]] = []
        if result.intent == Intent.PREEMPT and relates_to:
            status_updates.append((relates_to, Status.DEFERRED))
        elif result.intent in (Intent.SCOPE_CHANGE, Intent.NEW_GOAL) and relates_to:
            status_updates.append((relates_to, Status.SUPERSEDED))
        elif result.intent == Intent.NEW_GOAL and not relates_to:
            active = next(
                (
                    e
                    for e in reversed(entries)
                    if e.provenance == Provenance.HUMAN and e.status == Status.ACTIVE
                ),
                None,
            )
            if active is not None:
                status_updates.append((active.entry_id, Status.SUPERSEDED))
                relates_to = active.entry_id
        status = Status.SATISFIED if result.intent in _RECORD_INTENTS else Status.ACTIVE
        entry = GoalEntry(
            entry_id=entry_id,
            thread_id=job.thread_id,
            seq=0,
            verbatim=job.verbatim,
            provenance=job.provenance,
            channel=job.channel,
            intent=result.intent,
            status=status,
            relates_to=relates_to,
            constraints=constraints,
            done_when=(),
            created_at_ms=now_ms(),
        )
        stamped = await self._store.commit_classified(entry, status_updates=status_updates)
        await self._prune(job.thread_id)
        typed = sum(1 for c in constraints if c.kind != ConstraintKind.FREE_TEXT)
        logger.info(
            "goal_ledger classified %s",
            kv(
                thread_id=job.thread_id,
                seq=stamped.seq,
                intent=result.intent.value,
                typed_constraints=typed,
                free_text_constraints=len(constraints) - typed,
            ),
        )

    async def _prune(self, thread_id: str) -> None:
        entries = await self._store.list_entries(thread_id)
        if len(entries) <= self._max_entries:
            return
        extra = len(entries) - self._max_entries
        unconstrained = [e for e in entries if e.status in _TERMINAL_STATUSES and not e.constraints]
        constrained = [e for e in entries if e.status in _TERMINAL_STATUSES and e.constraints]
        droppable = unconstrained + constrained
        for entry in droppable[:extra]:
            await self._store.delete_entry(entry.entry_id)

    async def _refresh_view(self, thread_id: str) -> None:
        entries = await self._store.list_entries(thread_id)
        pending = self._pending.get(thread_id, 0) > 0
        view = resolve_intent(entries, pending_classification=pending)
        self._cache[thread_id] = view
        self._cache.move_to_end(thread_id)
        while len(self._cache) > self._thread_cap:
            self._cache.popitem(last=False)

    def _touch_pending_view(self, thread_id: str) -> None:
        view = self._cache.get(thread_id)
        if view is None:
            self._cache[thread_id] = empty_resolved(pending=True)
        elif not view.pending_classification:
            self._cache[thread_id] = replace(view, pending_classification=True)
        self._cache.move_to_end(thread_id)
        while len(self._cache) > self._thread_cap:
            self._cache.popitem(last=False)


def format_compaction_facts(view: ResolvedIntent) -> str:
    lines = [
        "Ledger facts (authoritative; do not contradict these in Objective or Important Details):",
    ]
    if view.active_goal is not None:
        lines.append(f"- Objective: {view.active_goal.verbatim}")
    for constraint in view.standing_constraints:
        lines.append(
            f"- Constraint ({constraint.kind.value} `{constraint.pattern}`): {constraint.verbatim}"
        )
    for deferred in view.deferred_stack:
        lines.append(f"- Deferred: {deferred.verbatim}")
    if view.active_goal is None and not view.standing_constraints:
        lines.append("- (none)")
    return "\n".join(lines)


def format_subagent_context(view: ResolvedIntent) -> str:
    lines = ["Parent goal ledger:"]
    if view.active_goal is not None:
        lines.append(f"Active goal: {view.active_goal.verbatim}")
    for constraint in view.standing_constraints:
        lines.append(
            f"Constraint: {constraint.verbatim} ({constraint.kind.value} {constraint.pattern})"
        )
    return "\n".join(lines)
