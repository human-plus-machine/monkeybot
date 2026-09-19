"""Off-loop judge worker. The hook only enqueues; settlement never waits here."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections import OrderedDict
from collections.abc import Callable
from contextlib import aclosing
from typing import Any, cast

from monkeybot.core.config.settings import VerifierJudgeConfig
from monkeybot.core.llm.provider import Done, Message, Provider, TextDelta, UsageEvent
from monkeybot.core.logging_utils import kv
from monkeybot.core.runtime.events import VerifierVerdict
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.verifier.ledger import GoalLedger
from monkeybot.core.verifier.mailbox import VerdictMailbox
from monkeybot.core.verifier.match import verdict_status
from monkeybot.core.verifier.port import EvidenceBundle, VerifierPort

logger = logging.getLogger(__name__)

_QUEUE_CAP = 32
_STATE_CAP = 256


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
        self._mailbox.mark_pending(thread_id)

    async def _run(self) -> None:
        while True:
            evidence = await self._queue.get()
            try:
                await self._handle(evidence)
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
                self._mailbox.clear_pending(evidence.thread_id)

    async def _handle(self, evidence: EvidenceBundle) -> None:
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
            logger.warning(
                "judge skipped non-verdict %s",
                kv(thread_id=evidence.thread_id, type=type(verdict).__name__),
            )
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
            correction=(
                "[Verifier] " + ", ".join(evidence.signals) + ". Stay on the user's stated goal."
                if evidence.signals
                else None
            ),
            judge_tokens=0,
        )


_JUDGE_TIMEOUT_S = 15.0
_JUDGE_STATUSES = frozenset({"on_track", "drifting", "stuck"})
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_JUDGE_SYSTEM = """\
You are a background verifier. Given the agent's current goal and tracker signals,
return ONLY compact JSON with this schema:
{"status":"on_track|drifting|stuck","severity":"none|nudge|replan|steer|block",\
"confidence":0.0,"rationale":"...","correction":"<short steering note or null>"}

Rules:
- status is required. Use on_track when the signals are noise, drifting when the
  agent is veering off the stated goal, stuck when it is looping or blocked.
- severity is required. Prefer none for on_track. Prefer nudge when a short
  correction would help. Use replan/block only for severe, repeated drift.
- correction is a one-sentence instruction the agent should follow next turn, or null.
- Do not invent facts that are not in the goal or signals.
"""


def parse_judge_verdict(raw: str) -> dict[str, Any] | None:
    """Extract a validated judge JSON object, or None if unusable."""
    from monkeybot.core.config.settings import VERIFIER_SEVERITY_ORDER

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
    severity = str(payload.get("severity") or "").strip()
    if severity not in VERIFIER_SEVERITY_ORDER:
        severity = "none" if status == "on_track" else "nudge"
    confidence_raw = payload.get("confidence", 0.6)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.6
    confidence = min(1.0, max(0.0, confidence))
    rationale = str(payload.get("rationale") or "").strip()
    corr_raw = payload.get("correction")
    correction = str(corr_raw).strip() if corr_raw not in (None, "", "null") else None
    return {
        "status": status,
        "severity": severity,
        "confidence": confidence,
        "rationale": rationale,
        "correction": correction,
    }


class ProviderJudge:
    """One small provider call per tracker emission. Raises so the worker fails open."""

    def __init__(
        self,
        provider: Provider | Callable[[], Provider | None] | None,
        *,
        model: str | Callable[[], str],
        timeout_s: float = _JUDGE_TIMEOUT_S,
    ) -> None:
        self._provider = provider
        self._model = model
        self._timeout_s = timeout_s

    def _current_provider(self) -> Provider | None:
        provider = self._provider
        if callable(provider):
            return provider()
        return provider

    def _current_model(self) -> str:
        model = self._model
        if callable(model):
            return (model() or "").strip()
        return (model or "").strip()

    async def verify(self, intent: object, evidence: EvidenceBundle) -> VerifierVerdict:
        provider = self._current_provider()
        model = self._current_model()
        if provider is None or not model:
            logger.warning(
                "verifier judge skipped %s",
                kv(has_provider=provider is not None, model=model or ""),
            )
            raise RuntimeError("verifier judge skipped: no provider or model")
        messages = [
            Message(role="system", content=[Text(text=_JUDGE_SYSTEM)]),
            Message(
                role="user",
                content=[Text(text=_judge_user_blob(intent, evidence))],
            ),
        ]
        try:
            text, tokens = await asyncio.wait_for(
                _collect_judge_text(provider, messages, model),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "verifier judge timed out %s",
                kv(model=model, timeout_s=self._timeout_s),
            )
            raise
        parsed = parse_judge_verdict(text)
        if parsed is None:
            logger.warning(
                "verifier judge unparseable %s",
                kv(model=model, chars=len(text)),
            )
            raise RuntimeError("verifier judge returned unparseable verdict")
        if tokens <= 0:
            logger.info("verifier judge missing usage %s", kv(model=model, chars=len(text)))
        correction = parsed["correction"]
        if correction is None and parsed["severity"] != "none":
            correction = (
                "[Verifier] " + ", ".join(evidence.signals) + ". Stay on the user's stated goal."
                if evidence.signals
                else None
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
            correction=correction,
            judge_tokens=max(0, tokens),
        )


async def _collect_judge_text(
    provider: Provider,
    messages: list[Message],
    model: str,
) -> tuple[str, int]:
    text = ""
    tokens = 0
    async with aclosing(cast(Any, provider.stream(messages, [], model=model))) as stream:
        async for ev in stream:
            if isinstance(ev, TextDelta):
                text += ev.text
            elif isinstance(ev, UsageEvent):
                tokens += max(0, ev.input_tokens) + max(0, ev.output_tokens)
            elif isinstance(ev, Done):
                break
    return text, tokens


def _judge_user_blob(intent: object, evidence: EvidenceBundle) -> str:
    goal = "(none)"
    constraints = "(none)"
    active = getattr(intent, "active_goal", None)
    if active is not None:
        verbatim = str(getattr(active, "verbatim", "") or "").strip()
        if verbatim:
            goal = verbatim[:480]
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
