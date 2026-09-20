"""Off-loop judge worker. The hook only enqueues; settlement never waits here."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
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
from monkeybot.core.verifier.mailbox import ScopeKey, VerdictMailbox
from monkeybot.core.verifier.match import verdict_status
from monkeybot.core.verifier.port import EvidenceBundle, VerifierPort

logger = logging.getLogger(__name__)

_QUEUE_CAP = 32
_MAX_IN_FLIGHT = 8
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
        self._jobs: set[asyncio.Task[None]] = set()
        self._sema: asyncio.Semaphore | None = None
        self._closed = False
        self._verdicts_this_request: OrderedDict[ScopeKey, int] = OrderedDict()
        self._last_turn: OrderedDict[ScopeKey, int] = OrderedDict()
        self._spend: OrderedDict[ScopeKey, int] = OrderedDict()
        self._agent_spend: OrderedDict[ScopeKey, int] = OrderedDict()

    def close(self) -> None:
        self._closed = True
        jobs = list(self._jobs)
        self._jobs.clear()
        for task in jobs:
            task.cancel()

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
        agent = self._agent_spend.get(key, 0)
        spend = self._spend.get(key, 0)
        if agent > 0 and spend / agent > self._config.max_spend_ratio:
            logger.warning(
                "judge skip spend_ratio %s",
                kv(thread_id=thread_id, request_id=request_id, spend=spend, agent=agent),
            )
            return
        if len(self._jobs) >= _QUEUE_CAP:
            logger.warning(
                "judge queue full %s",
                kv(
                    thread_id=thread_id,
                    request_id=request_id,
                    queue_depth=len(self._jobs),
                ),
            )
            return
        # Count the attempt now, not when the call returns: a slow port would
        # otherwise let every in-flight turn past both rate limits.
        self._bump(self._verdicts_this_request, key, 1)
        self._store(self._last_turn, key, evidence.inner_turn)
        self._mailbox.mark_pending(thread_id, request_id)
        queued_at = time.monotonic()
        task = asyncio.create_task(self._run_one(evidence, queued_at), name="verifier-judge")
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)
        logger.info(
            "judge enqueue %s",
            kv(
                thread_id=thread_id,
                request_id=request_id,
                queue_depth=len(self._jobs),
                inner_turn=evidence.inner_turn,
            ),
        )

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sema is None:
            self._sema = asyncio.Semaphore(_MAX_IN_FLIGHT)
        return self._sema

    async def _run_one(self, evidence: EvidenceBundle, queued_at: float) -> None:
        acquired = False
        try:
            if self._closed:
                self._refund(evidence)
                return
            await self._semaphore().acquire()
            acquired = True
            wait_ms = int((time.monotonic() - queued_at) * 1000)
            logger.info(
                "judge start %s",
                kv(
                    thread_id=evidence.thread_id,
                    request_id=evidence.request_id,
                    queue_wait_ms=wait_ms,
                    queue_depth=len(self._jobs),
                ),
            )
            if self._closed:
                self._refund(evidence)
                return
            await self._handle(evidence)
        except asyncio.CancelledError:
            self._refund(evidence)
            raise
        except Exception:
            self._refund(evidence)
            logger.warning(
                "judge handle failed %s",
                kv(thread_id=evidence.thread_id, request_id=evidence.request_id),
                exc_info=True,
            )
        finally:
            if acquired:
                self._semaphore().release()
            self._mailbox.clear_pending(evidence.thread_id, evidence.request_id)

    async def _handle(self, evidence: EvidenceBundle) -> None:
        from monkeybot.core.verifier.binding import bind_verifier_session, reset_verifier_session

        token = bind_verifier_session(evidence.provider, evidence.model)
        try:
            await self._verify_and_deposit(evidence)
        finally:
            reset_verifier_session(token)

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
            logger.warning(
                "judge skipped non-verdict %s",
                kv(thread_id=evidence.thread_id, type=type(verdict).__name__),
            )
            return
        self._mailbox.put(evidence.thread_id, verdict)
        key = (evidence.thread_id, evidence.request_id)
        self._bump(self._spend, key, max(0, verdict.judge_tokens))
        agent = self._agent_spend.get(key, 0)
        logger.info(
            "judge spend %s",
            kv(
                thread_id=evidence.thread_id,
                request_id=evidence.request_id,
                judge_tokens=self._spend[key],
                agent_tokens=agent,
            ),
        )

    def _refund(self, evidence: EvidenceBundle) -> None:
        """Give back the verdict budget charged at enqueue when no verdict landed."""
        key = (evidence.thread_id, evidence.request_id)
        charged = self._verdicts_this_request.get(key, 0)
        if charged > 0:
            self._store(self._verdicts_this_request, key, charged - 1)
        last = self._last_turn.get(key)
        if last == evidence.inner_turn:
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
        from monkeybot.core.verifier.intervention import correction_text

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


_JUDGE_TIMEOUT_S = 15.0
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


def parse_judge_verdict(raw: str) -> dict[str, Any] | None:
    """Extract a validated judge JSON object, or None if unusable.

    Accepted combinations after normalization:
    - ``on_track`` → ``severity="none"`` (model severity is ignored)
    - ``drifting`` / ``stuck`` → ``nudge`` / ``replan`` / ``steer`` / ``block``
      (missing, invalid, or ``none`` severity becomes ``nudge``)

    Model ``correction`` is discarded. Callers own trusted actuation text.
    """
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
    confidence = min(1.0, max(0.0, confidence))
    rationale = str(payload.get("rationale") or "").strip()
    return {
        "status": status,
        "severity": severity,
        "confidence": confidence,
        "rationale": rationale,
        "correction": None,
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
                _collect_judge_text(provider, messages, model, evidence),
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
            raise
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
            raise RuntimeError("verifier judge returned unparseable verdict")
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
        from monkeybot.core.verifier.intervention import correction_text

        correction = correction_text(evidence.signals) if parsed["severity"] != "none" else None
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
    evidence: EvidenceBundle,
) -> tuple[str, int]:
    from monkeybot.core.context import TurnContext
    from monkeybot.observability.spans import set_llm_io, set_llm_usage, span_llm

    prompt = "\n".join(
        "".join(b.text for b in message.content if isinstance(b, Text)) for message in messages
    )
    ctx = TurnContext(
        thread_id=evidence.thread_id,
        request_id=evidence.request_id,
        agent_md="",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model=model,
    )
    started = time.monotonic()
    text = ""
    input_tokens = 0
    output_tokens = 0
    try:
        async with span_llm(ctx=ctx, model=model):
            async with aclosing(cast(Any, provider.stream(messages, [], model=model))) as stream:
                async for ev in stream:
                    if isinstance(ev, TextDelta):
                        text += ev.text
                    elif isinstance(ev, UsageEvent):
                        input_tokens += max(0, ev.input_tokens)
                        output_tokens += max(0, ev.output_tokens)
                    elif isinstance(ev, Done):
                        break
            set_llm_io(prompt=prompt, completion=text)
            if input_tokens or output_tokens:
                set_llm_usage(input_tokens=input_tokens, output_tokens=output_tokens)
    except Exception:
        logger.warning(
            "verifier judge stream failed %s",
            kv(
                thread_id=evidence.thread_id,
                request_id=evidence.request_id,
                model=model,
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
            exc_info=True,
        )
        raise
    tokens = input_tokens + output_tokens
    logger.info(
        "verifier judge stream %s",
        kv(
            thread_id=evidence.thread_id,
            request_id=evidence.request_id,
            model=model,
            duration_ms=int((time.monotonic() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            chars=len(text),
        ),
    )
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
