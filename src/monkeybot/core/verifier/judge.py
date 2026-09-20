"""Off-loop judge worker. The hook only enqueues; settlement never waits here."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TypedDict

from monkeybot.core.config.settings import VERIFIER_SEVERITY_ORDER, VerifierJudgeConfig
from monkeybot.core.llm.provider import Message
from monkeybot.core.logging_utils import kv
from monkeybot.core.runtime.events import VerifierVerdict
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.verifier.binding import (
    ModelRef,
    ProviderRef,
    resolve_live_model,
    resolve_live_provider,
)
from monkeybot.core.verifier.classify import collect_verifier_stream
from monkeybot.core.verifier.intervention import correction_text
from monkeybot.core.verifier.ledger import GoalLedger
from monkeybot.core.verifier.mailbox import ScopeKey, VerdictMailbox
from monkeybot.core.verifier.match import verdict_status
from monkeybot.core.verifier.port import EvidenceBundle, VerifierPort

logger = logging.getLogger(__name__)

_STATE_CAP = 256
_IDLE_SLOT_S = 16.0
_IDLE_CEILING_S = 64.0
_JUDGE_TIMEOUT_S = 15.0
_RATIONALE_MAX = 480
_JUDGE_STATUSES = frozenset({"on_track", "drifting", "stuck"})
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_JUDGE_SYSTEM = """\
You are a background verifier. Given the agent's current goal and tracker signals,
return ONLY compact JSON with this schema:
{"status":"on_track|drifting|stuck","severity":"none|nudge|replan|steer|block",\
"confidence":0.0,"rationale":"..."}

Rules:
- status is required. Use on_track when the signals are noise, drifting when the
  agent is veering off the stated goal, stuck when it is looping or blocked.
- severity is required. Prefer none for on_track. Prefer nudge when a short
  correction would help. Use replan/block only for severe, repeated drift.
- If status is on_track, severity MUST be none.
- rationale is a short telemetry note for operators. It is never shown to the agent.
- Do not invent facts that are not in the goal or signals.
"""


@dataclass
class _JudgeJob:
    evidence: EvidenceBundle
    prev_turn: int
    queued: bool = True
    pending: bool = True
    refunded: bool = False
    deposited: bool = False

    @property
    def key(self) -> ScopeKey:
        return (self.evidence.thread_id, self.evidence.request_id)

    def release_pending(self, mailbox: VerdictMailbox) -> None:
        if not self.pending:
            return
        self.pending = False
        mailbox.clear_pending(self.evidence.thread_id, self.evidence.request_id)


class JudgeWorker:
    """Per-process worker that calls ``VerifierPort`` and deposits mailbox verdicts."""

    def __init__(
        self,
        mailbox: VerdictMailbox,
        port: VerifierPort,
        *,
        ledger_fn: Callable[[], GoalLedger | None],
        config: VerifierJudgeConfig,
    ) -> None:
        self._mailbox = mailbox
        self._port = port
        self._ledger_fn = ledger_fn
        self._config = config
        self._jobs: dict[asyncio.Task[None], _JudgeJob] = {}
        self._sema: asyncio.Semaphore | None = None
        self._scope_locks: OrderedDict[ScopeKey, asyncio.Lock] = OrderedDict()
        self._scope_lock_holders: dict[ScopeKey, int] = {}
        self._queued = 0
        self._closed = False
        self._verdicts_this_request: OrderedDict[ScopeKey, int] = OrderedDict()
        self._last_turn: OrderedDict[ScopeKey, int] = OrderedDict()
        self._spend: OrderedDict[ScopeKey, int] = OrderedDict()
        self._agent_spend: OrderedDict[ScopeKey, int] = OrderedDict()

    def close(self) -> None:
        """Cancel in-flight jobs and drop their pending marks without awaiting."""
        self._closed = True
        jobs = dict(self._jobs)
        self._jobs.clear()
        if jobs:
            logger.info("judge close cancelling %s", kv(jobs=len(jobs)))
        for task, job in jobs.items():
            task.cancel()
            job.release_pending(self._mailbox)
            if not job.deposited:
                self._refund(job)

    async def aclose(self) -> None:
        jobs = list(self._jobs)
        self.close()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    async def wait_idle(self) -> int:
        """Wait for in-flight jobs to finish so a reload can deposit their verdicts.

        Jobs run concurrently up to ``max_in_flight``, so the deadline is waves
        of one judge slot, capped so a wedged port cannot hold the reload lock.
        Returns the number of jobs observed at the start of the wait.
        """
        jobs = [task for task in self._jobs if not task.done()]
        if not jobs:
            return 0
        inflight = max(1, self._config.max_in_flight)
        waves = max(1, (len(jobs) + inflight - 1) // inflight)
        _done, pending = await asyncio.wait(
            jobs,
            timeout=min(_IDLE_CEILING_S, waves * _IDLE_SLOT_S),
        )
        if pending:
            raise TimeoutError
        return len(jobs)

    def note_agent_tokens(self, thread_id: str, request_id: str, tokens: int) -> None:
        self._bump(self._agent_spend, (thread_id, request_id), tokens)

    def enqueue(self, evidence: EvidenceBundle) -> None:
        if self._closed:
            return
        request_id = evidence.request_id
        thread_id = evidence.thread_id
        key = (thread_id, request_id)
        if self._verdicts_this_request.get(key, 0) >= self._config.max_verdicts_per_message:
            logger.info(
                "judge skip rate_limit %s",
                kv(thread_id=thread_id, request_id=request_id, reason="max_verdicts_per_message"),
            )
            return
        last = self._last_turn.get(key, 0)
        if last > 0 and evidence.inner_turn - last < self._config.min_turns_between_verdicts:
            logger.info(
                "judge skip rate_limit %s",
                kv(
                    thread_id=thread_id,
                    request_id=request_id,
                    reason="min_turns_between_verdicts",
                ),
            )
            return
        if self._spend_limit_reached(key, phase="enqueue"):
            return
        if self._queued >= self._config.queue_cap:
            logger.warning(
                "judge queue full %s",
                kv(
                    thread_id=thread_id,
                    request_id=request_id,
                    queue_depth=self._queued,
                    in_flight=len(self._jobs),
                ),
            )
            return
        # Count the attempt now, not when the call returns: a slow port would
        # otherwise let every in-flight turn past both rate limits.
        prev_turn = self._last_turn.get(key, 0)
        self._bump(self._verdicts_this_request, key, 1)
        self._store(self._last_turn, key, evidence.inner_turn)
        self._mailbox.mark_pending(thread_id, request_id)
        self._queued += 1
        job = _JudgeJob(evidence=evidence, prev_turn=prev_turn)
        queued_at = time.monotonic()
        task = asyncio.create_task(self._run_one(job, queued_at), name="verifier-judge")
        self._jobs[task] = job
        task.add_done_callback(self._forget_job)
        logger.info(
            "judge enqueue %s",
            kv(
                thread_id=thread_id,
                request_id=request_id,
                queue_depth=self._queued,
                inner_turn=evidence.inner_turn,
            ),
        )

    def _forget_job(self, task: asyncio.Task[None]) -> None:
        self._jobs.pop(task, None)

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sema is None:
            self._sema = asyncio.Semaphore(self._config.max_in_flight)
        return self._sema

    def _scope_lock(self, key: ScopeKey) -> asyncio.Lock:
        """Serialize this scope's jobs so each one sees the spend of the ones before it.

        Holders are refcounted so eviction cannot drop a lock a created-but-not-
        yet-started job still plans to acquire.
        """
        lock = self._scope_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._scope_locks[key] = lock
            self._scope_lock_holders[key] = 0
        self._scope_lock_holders[key] = self._scope_lock_holders.get(key, 0) + 1
        self._scope_locks.move_to_end(key)
        while len(self._scope_locks) > _STATE_CAP:
            victim = next(
                (
                    k
                    for k in self._scope_locks
                    if k != key and self._scope_lock_holders.get(k, 0) == 0
                ),
                None,
            )
            if victim is None:
                break
            self._scope_locks.pop(victim, None)
            self._scope_lock_holders.pop(victim, None)
        return lock

    def _release_scope_lock(self, key: ScopeKey) -> None:
        holders = self._scope_lock_holders.get(key, 0) - 1
        if holders > 0:
            self._scope_lock_holders[key] = holders
            return
        self._scope_lock_holders.pop(key, None)

    def _spend_limit_reached(self, key: ScopeKey, *, phase: str) -> bool:
        agent = self._agent_spend.get(key, 0)
        spend = self._spend.get(key, 0)
        if agent <= 0 or spend / agent <= self._config.max_spend_ratio:
            return False
        logger.warning(
            "judge skip spend_ratio %s",
            kv(thread_id=key[0], request_id=key[1], phase=phase, spend=spend, agent=agent),
        )
        return True

    def _dequeue(self, job: _JudgeJob) -> None:
        if not job.queued:
            return
        job.queued = False
        self._queued = max(0, self._queued - 1)

    def _bail(self, job: _JudgeJob, *, phase: str) -> bool:
        """Refund and return True when this job must not call the provider."""
        if not self._closed and not self._spend_limit_reached(job.key, phase=phase):
            return False
        self._refund(job)
        return True

    async def _run_one(self, job: _JudgeJob, queued_at: float) -> None:
        evidence = job.evidence
        key = job.key
        lock: asyncio.Lock | None = None
        try:
            if self._bail(job, phase="before_start"):
                return
            lock = self._scope_lock(key)
            async with lock:
                if self._bail(job, phase="before_start"):
                    return
                async with self._semaphore():
                    self._dequeue(job)
                    logger.info(
                        "judge start %s",
                        kv(
                            thread_id=evidence.thread_id,
                            request_id=evidence.request_id,
                            queue_wait_ms=int((time.monotonic() - queued_at) * 1000),
                            queue_depth=self._queued,
                        ),
                    )
                    if self._bail(job, phase="before_start"):
                        return
                    await self._verify_and_deposit(job)
        except asyncio.CancelledError:
            self._refund(job)
            raise
        except Exception:
            self._refund(job)
            logger.warning(
                "judge handle failed %s",
                kv(thread_id=evidence.thread_id, request_id=evidence.request_id),
                exc_info=True,
            )
        finally:
            self._dequeue(job)
            if lock is not None:
                self._release_scope_lock(key)
            job.release_pending(self._mailbox)

    async def _verify_and_deposit(self, job: _JudgeJob) -> None:
        evidence = job.evidence
        ledger = self._ledger_fn()
        intent = ledger.resolved_intent(evidence.thread_id) if ledger is not None else None
        try:
            verdict = await self._port.verify(intent, evidence)
        except Exception:
            self._refund(job)
            logger.warning(
                "judge failed %s",
                kv(thread_id=evidence.thread_id, request_id=evidence.request_id),
                exc_info=True,
            )
            return
        if not isinstance(verdict, VerifierVerdict):
            self._refund(job)
            return
        if evidence.signal_epochs and not verdict.triggering_signal_epochs:
            verdict = replace(verdict, triggering_signal_epochs=evidence.signal_epochs)
        self._mailbox.put(evidence.thread_id, verdict)
        job.deposited = True
        self._bump(self._spend, job.key, max(0, verdict.judge_tokens))
        agent = self._agent_spend.get(job.key, 0)
        logger.info(
            "judge spend %s",
            kv(
                thread_id=evidence.thread_id,
                request_id=evidence.request_id,
                judge_tokens=self._spend[job.key],
                agent_tokens=agent,
            ),
        )

    def _refund(self, job: _JudgeJob) -> None:
        """Give back the verdict budget charged at enqueue when no verdict landed."""
        if job.refunded or job.deposited:
            return
        job.refunded = True
        key = job.key
        charged = self._verdicts_this_request.get(key, 0)
        if charged > 0:
            self._store(self._verdicts_this_request, key, charged - 1)
        last = self._last_turn.get(key)
        if last != job.evidence.inner_turn:
            return
        if job.prev_turn > 0:
            self._store(self._last_turn, key, job.prev_turn)
        else:
            self._last_turn.pop(key, None)

    @staticmethod
    def _store(store: OrderedDict[ScopeKey, int], key: ScopeKey, value: int) -> None:
        store[key] = value
        store.move_to_end(key)
        while len(store) > _STATE_CAP:
            store.popitem(last=False)

    def _bump(self, store: OrderedDict[ScopeKey, int], key: ScopeKey, delta: int) -> None:
        self._store(store, key, store.get(key, 0) + delta)


class SignalJudge:
    """Deterministic VerifierPort: map tracker signals to a nudge. Fail-open caller."""

    async def verify(self, intent: object, evidence: EvidenceBundle) -> VerifierVerdict:
        del intent
        status, confidence = verdict_status(evidence.signals)
        return VerifierVerdict(
            request_id=evidence.request_id,
            verdict_id=str(uuid.uuid4()),
            checkpoint_id=f"{evidence.request_id}:{evidence.inner_turn}",
            status=status,
            severity="nudge",
            confidence=confidence,
            rationale=", ".join(evidence.signals),
            triggering_signals=evidence.signals,
            triggering_signal_epochs=evidence.signal_epochs,
            correction=correction_text(evidence.signals) if evidence.signals else None,
            judge_tokens=0,
        )


class _ParsedJudge(TypedDict):
    status: str
    severity: str
    confidence: float
    rationale: str


def parse_judge_verdict(raw: str) -> _ParsedJudge | None:
    """Extract a validated judge JSON object, or None if unusable.

    Accepted combinations after normalization:
    - ``on_track`` → ``severity="none"`` (model severity is ignored)
    - ``drifting`` / ``stuck`` → ``nudge`` / ``replan`` / ``steer`` / ``block``
      (missing, invalid, or ``none`` severity becomes ``nudge``)

    Model ``correction`` is discarded. Callers own trusted actuation text.
    """
    text = raw.strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    status = str(payload.get("status") or "").strip()
    if status not in _JUDGE_STATUSES:
        return None
    # on_track is never actuating: ignore a model that pairs it with nudge/replan/block.
    if status == "on_track":
        severity = "none"
    else:
        severity = str(payload.get("severity") or "").strip()
        if severity not in VERIFIER_SEVERITY_ORDER or severity == "none":
            severity = "nudge"
    confidence_raw = payload.get("confidence", 0.6)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.6
    confidence = 0.6 if not math.isfinite(confidence) else min(1.0, max(0.0, confidence))
    rationale = str(payload.get("rationale") or "").strip()[:_RATIONALE_MAX]
    return {
        "status": status,
        "severity": severity,
        "confidence": confidence,
        "rationale": rationale,
    }


class ProviderJudge:
    """One small provider call per tracker emission. Returns None so the worker fails open."""

    def __init__(
        self,
        provider: ProviderRef,
        *,
        model: ModelRef,
        timeout_s: float = _JUDGE_TIMEOUT_S,
    ) -> None:
        self._provider = provider
        self._model = model
        self._timeout_s = timeout_s

    async def verify(self, intent: object, evidence: EvidenceBundle) -> VerifierVerdict | None:
        provider = evidence.provider or resolve_live_provider(self._provider)
        model = resolve_live_model(self._model, evidence.model)
        if provider is None or not model:
            logger.warning(
                "verifier judge skipped %s",
                kv(has_provider=provider is not None, model=model or ""),
            )
            return None
        try:
            text, tokens = await asyncio.wait_for(
                collect_verifier_stream(
                    provider,
                    _judge_messages(intent, evidence),
                    model,
                    thread_id=evidence.thread_id,
                    request_id=evidence.request_id,
                    log_event="verifier judge stream",
                    log=logger,
                ),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "verifier judge timed out %s",
                kv(
                    thread_id=evidence.thread_id,
                    request_id=evidence.request_id,
                    model=model,
                    timeout_s=self._timeout_s,
                ),
            )
            return None
        parsed = parse_judge_verdict(text)
        if parsed is None:
            logger.warning(
                "verifier judge unparseable %s",
                kv(
                    thread_id=evidence.thread_id,
                    request_id=evidence.request_id,
                    model=model,
                    chars=len(text),
                ),
            )
            return None
        if tokens <= 0:
            logger.info(
                "verifier judge missing usage %s",
                kv(
                    thread_id=evidence.thread_id,
                    request_id=evidence.request_id,
                    model=model,
                    chars=len(text),
                ),
            )
        return VerifierVerdict(
            request_id=evidence.request_id,
            verdict_id=str(uuid.uuid4()),
            checkpoint_id=f"{evidence.request_id}:{evidence.inner_turn}",
            status=parsed["status"],
            severity=parsed["severity"],
            confidence=parsed["confidence"],
            rationale=parsed["rationale"] or ", ".join(evidence.signals),
            triggering_signals=evidence.signals,
            triggering_signal_epochs=evidence.signal_epochs,
            correction=correction_text(evidence.signals) if parsed["severity"] != "none" else None,
            judge_tokens=max(0, tokens),
        )


def _judge_messages(intent: object, evidence: EvidenceBundle) -> list[Message]:
    return [
        Message(role="system", content=[Text(text=_JUDGE_SYSTEM)]),
        Message(role="user", content=[Text(text=_judge_user_blob(intent, evidence))]),
    ]


def _judge_user_blob(intent: object, evidence: EvidenceBundle) -> str:
    goal = "(none)"
    constraints = "(none)"
    active = getattr(intent, "active_goal", None)
    if active is not None:
        verbatim = str(getattr(active, "verbatim", "") or "").strip()
        if verbatim:
            goal = verbatim[:_RATIONALE_MAX]
    standing = getattr(intent, "standing_constraints", None) or ()
    if standing:
        lines: list[str] = []
        for item in standing:
            kind = getattr(getattr(item, "kind", None), "value", None) or getattr(item, "kind", "")
            pattern = getattr(item, "pattern", "")
            lines.append(f"- {kind}:{pattern}")
        constraints = "\n".join(lines)
    signals = ", ".join(evidence.signals) or "(none)"
    return (
        f"Active goal:\n{goal}\n\nStanding constraints:\n{constraints}\n\n"
        f"Tracker signals: {signals}\nInner turn: {evidence.inner_turn}"
    )
