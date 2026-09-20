"""Off-loop judge worker. The hook only enqueues; settlement never waits here."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import uuid
from collections import OrderedDict
from collections.abc import Callable
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
from monkeybot.core.verifier.mailbox import VerdictMailbox
from monkeybot.core.verifier.match import verdict_status
from monkeybot.core.verifier.port import EvidenceBundle, VerifierPort

logger = logging.getLogger(__name__)

_QUEUE_CAP = 32
_STATE_CAP = 256
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
        self._queue: asyncio.Queue[EvidenceBundle] = asyncio.Queue(maxsize=_QUEUE_CAP)
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._verdicts_this_request: OrderedDict[str, int] = OrderedDict()
        self._last_turn: OrderedDict[str, int] = OrderedDict()
        self._spend: OrderedDict[str, int] = OrderedDict()
        self._agent_spend: OrderedDict[str, int] = OrderedDict()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="verifier-judge")

    def close(self) -> None:
        self._closed = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()

    def note_agent_tokens(self, request_id: str, tokens: int) -> None:
        self._bump(self._agent_spend, request_id, tokens)

    def enqueue(self, evidence: EvidenceBundle) -> None:
        if self._closed:
            return
        self.start()
        request_id = evidence.request_id
        thread_id = evidence.thread_id
        if self._verdicts_this_request.get(request_id, 0) >= self._config.max_verdicts_per_message:
            logger.info(
                "judge skip rate_limit %s",
                kv(thread_id=thread_id, reason="max_verdicts_per_message"),
            )
            return
        last = self._last_turn.get(request_id, 0)
        if last > 0 and evidence.inner_turn - last < self._config.min_turns_between_verdicts:
            logger.info(
                "judge skip rate_limit %s",
                kv(thread_id=thread_id, reason="min_turns_between_verdicts"),
            )
            return
        agent = self._agent_spend.get(request_id, 0)
        spend = self._spend.get(request_id, 0)
        if agent > 0 and spend / agent > self._config.max_spend_ratio:
            logger.warning(
                "judge skip spend_ratio %s",
                kv(request_id=request_id, spend=spend, agent=agent),
            )
            return
        try:
            self._queue.put_nowait(evidence)
        except asyncio.QueueFull:
            logger.warning("judge queue full %s", kv(thread_id=thread_id))
            return
        # Count the attempt now, not when the call returns: a slow port would
        # otherwise let every in-flight turn past both rate limits.
        self._bump(self._verdicts_this_request, request_id, 1)
        self._store(self._last_turn, request_id, evidence.inner_turn)
        self._mailbox.mark_pending(thread_id, request_id)

    async def _run(self) -> None:
        while True:
            evidence = await self._queue.get()
            try:
                await self._verify_and_deposit(evidence)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._refund(evidence)
                logger.warning(
                    "judge handle failed %s",
                    kv(thread_id=evidence.thread_id, request_id=evidence.request_id),
                    exc_info=True,
                )
            finally:
                self._mailbox.clear_pending(evidence.thread_id, evidence.request_id)

    async def _verify_and_deposit(self, evidence: EvidenceBundle) -> None:
        ledger = self._ledger_fn()
        intent = ledger.resolved_intent(evidence.thread_id) if ledger is not None else None
        try:
            verdict = await self._port.verify(intent, evidence)
        except Exception:
            self._refund(evidence)
            logger.warning(
                "judge failed %s",
                kv(thread_id=evidence.thread_id, request_id=evidence.request_id),
                exc_info=True,
            )
            return
        if not isinstance(verdict, VerifierVerdict):
            self._refund(evidence)
            return
        self._mailbox.put(evidence.thread_id, verdict)
        self._bump(self._spend, evidence.request_id, max(0, verdict.judge_tokens))
        agent = self._agent_spend.get(evidence.request_id, 0)
        logger.info(
            "judge spend %s",
            kv(
                request_id=evidence.request_id,
                judge_tokens=self._spend[evidence.request_id],
                agent_tokens=agent,
            ),
        )

    def _refund(self, evidence: EvidenceBundle) -> None:
        """Give back the verdict budget charged at enqueue when no verdict landed."""
        charged = self._verdicts_this_request.get(evidence.request_id, 0)
        if charged > 0:
            self._store(self._verdicts_this_request, evidence.request_id, charged - 1)

    @staticmethod
    def _store(store: OrderedDict[str, int], key: str, value: int) -> None:
        store[key] = value
        store.move_to_end(key)
        while len(store) > _STATE_CAP:
            store.popitem(last=False)

    def _bump(self, store: OrderedDict[str, int], key: str, delta: int) -> None:
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
