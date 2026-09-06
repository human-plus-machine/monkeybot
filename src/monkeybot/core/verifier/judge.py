"""Off-loop judge worker. The hook only enqueues; settlement never waits here."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from collections.abc import Callable

from monkeybot.core.config.settings import VerifierJudgeConfig
from monkeybot.core.logging_utils import kv
from monkeybot.core.runtime.events import VerifierVerdict
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

    async def _run(self) -> None:
        while True:
            evidence = await self._queue.get()
            try:
                await self._handle(evidence)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "judge handle failed %s",
                    kv(thread_id=evidence.thread_id, request_id=evidence.request_id),
                    exc_info=True,
                )

    async def _handle(self, evidence: EvidenceBundle) -> None:
        ledger = self._ledger_fn()
        intent = ledger.resolved_intent(evidence.thread_id) if ledger is not None else None
        try:
            verdict = await self._port.verify(intent, evidence)
        except Exception:
            logger.warning(
                "judge failed %s",
                kv(thread_id=evidence.thread_id, request_id=evidence.request_id),
                exc_info=True,
            )
            return
        if not isinstance(verdict, VerifierVerdict):
            logger.warning(
                "judge skipped non-verdict %s",
                kv(thread_id=evidence.thread_id, type=type(verdict).__name__),
            )
            return
        self._mailbox.put(evidence.thread_id, verdict)
        self._bump(self._verdicts_this_request, evidence.request_id, 1)
        self._store(self._last_turn, evidence.request_id, evidence.inner_turn)
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
