"""JudgeWorker rate limits, spend units, and fail-open worker loop."""

from __future__ import annotations

import asyncio

import pytest

from monkeybot.core.config.settings import VerifierJudgeConfig
from monkeybot.core.runtime.events import VerifierVerdict
from monkeybot.core.verifier.judge import _STATE_CAP, JudgeWorker, SignalJudge
from monkeybot.core.verifier.mailbox import VerdictMailbox
from monkeybot.core.verifier.port import EvidenceBundle


def _evidence(request_id: str, inner_turn: int, thread_id: str = "t1") -> EvidenceBundle:
    return EvidenceBundle(
        thread_id=thread_id,
        request_id=request_id,
        inner_turn=inner_turn,
        signals=("constraint_touch",),
    )


class _TokenPort:
    def __init__(self, tokens: int) -> None:
        self.tokens = tokens
        self.calls = 0

    async def verify(self, intent: object, evidence: EvidenceBundle) -> VerifierVerdict:
        del intent
        self.calls += 1
        return VerifierVerdict(
            request_id=evidence.request_id,
            verdict_id=f"v{self.calls}",
            severity="nudge",
            judge_tokens=self.tokens,
        )


class _CountingPort:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, intent: object, evidence: EvidenceBundle) -> VerifierVerdict:
        del intent
        self.calls += 1
        return VerifierVerdict(
            request_id=evidence.request_id,
            verdict_id=f"v{self.calls}",
            severity="nudge",
            judge_tokens=0,
        )


@pytest.mark.asyncio
async def test_spend_ratio_compares_judge_tokens_to_agent_tokens() -> None:
    mailbox = VerdictMailbox()
    port = _TokenPort(tokens=100)
    worker = JudgeWorker(
        mailbox,
        port,
        ledger_fn=lambda: None,
        config=VerifierJudgeConfig(max_spend_ratio=0.25, max_verdicts_per_message=10),
    )
    worker.note_agent_tokens("r1", 100)
    worker.enqueue(_evidence("r1", 1))
    await asyncio.sleep(0.05)
    assert len(mailbox.take_ready("t1")) == 1
    worker.enqueue(_evidence("r1", 4))
    await asyncio.sleep(0.05)
    assert mailbox.take_ready("t1") == []
    assert port.calls == 1
    worker.close()


@pytest.mark.asyncio
async def test_min_turns_is_per_request_not_thread() -> None:
    mailbox = VerdictMailbox()
    port = _CountingPort()
    worker = JudgeWorker(
        mailbox,
        port,
        ledger_fn=lambda: None,
        config=VerifierJudgeConfig(min_turns_between_verdicts=2, max_verdicts_per_message=10),
    )
    worker.enqueue(_evidence("r1", 5))
    await asyncio.sleep(0.05)
    assert len(mailbox.take_ready("t1")) == 1
    worker.enqueue(_evidence("r2", 1))
    await asyncio.sleep(0.05)
    assert len(mailbox.take_ready("t1")) == 1
    assert port.calls == 2
    worker.close()


@pytest.mark.asyncio
async def test_handle_error_does_not_kill_worker() -> None:
    mailbox = VerdictMailbox()
    calls = {"n": 0}

    def ledger_fn() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ledger down")
        return None

    port = _CountingPort()
    worker = JudgeWorker(
        mailbox,
        port,
        ledger_fn=ledger_fn,
        config=VerifierJudgeConfig(max_verdicts_per_message=10),
    )
    worker.enqueue(_evidence("r1", 1))
    await asyncio.sleep(0.05)
    assert mailbox.take_ready("t1") == []
    worker.enqueue(_evidence("r1", 4))
    await asyncio.sleep(0.05)
    assert len(mailbox.take_ready("t1")) == 1
    assert port.calls == 1
    worker.close()


class _SlowPort:
    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.calls = 0

    async def verify(self, intent: object, evidence: EvidenceBundle) -> VerifierVerdict:
        del intent
        self.calls += 1
        await asyncio.sleep(self.delay_s)
        return VerifierVerdict(
            request_id=evidence.request_id,
            verdict_id=f"v{self.calls}",
            severity="nudge",
            judge_tokens=0,
        )


@pytest.mark.asyncio
async def test_rate_limit_holds_while_a_judge_call_is_in_flight() -> None:
    """A slow port must not let every in-flight turn past max_verdicts_per_message."""
    mailbox = VerdictMailbox()
    port = _SlowPort(delay_s=0.2)
    worker = JudgeWorker(
        mailbox,
        port,
        ledger_fn=lambda: None,
        config=VerifierJudgeConfig(max_verdicts_per_message=1, min_turns_between_verdicts=0),
    )
    for turn in range(1, 6):
        worker.enqueue(_evidence("r1", turn))
    await asyncio.sleep(0.05)
    assert port.calls == 1
    await asyncio.sleep(0.4)
    assert port.calls == 1
    assert len(mailbox.take_ready("t1")) == 1
    worker.close()


@pytest.mark.asyncio
async def test_pending_is_marked_while_in_flight_and_cleared_after() -> None:
    mailbox = VerdictMailbox()
    worker = JudgeWorker(
        mailbox,
        _SlowPort(delay_s=0.1),
        ledger_fn=lambda: None,
        config=VerifierJudgeConfig(max_verdicts_per_message=10),
    )
    assert mailbox.pending("t1") is False
    worker.enqueue(_evidence("r1", 1))
    assert mailbox.pending("t1") is True
    await asyncio.sleep(0.3)
    assert mailbox.pending("t1") is False
    worker.close()


@pytest.mark.asyncio
async def test_failed_judge_call_refunds_the_verdict_budget() -> None:
    class _BoomPort:
        calls = 0

        async def verify(self, intent: object, evidence: EvidenceBundle) -> VerifierVerdict:
            del intent, evidence
            type(self).calls += 1
            raise RuntimeError("judge down")

    mailbox = VerdictMailbox()
    port = _BoomPort()
    worker = JudgeWorker(
        mailbox,
        port,
        ledger_fn=lambda: None,
        config=VerifierJudgeConfig(max_verdicts_per_message=1, min_turns_between_verdicts=0),
    )
    worker.enqueue(_evidence("r1", 1))
    await asyncio.sleep(0.05)
    assert _BoomPort.calls == 1
    assert mailbox.pending("t1") is False
    # The failed attempt was refunded, so the budget is available again.
    worker.enqueue(_evidence("r1", 2))
    await asyncio.sleep(0.05)
    assert _BoomPort.calls == 2
    worker.close()


@pytest.mark.asyncio
async def test_close_cancels_worker_task() -> None:
    mailbox = VerdictMailbox()
    worker = JudgeWorker(
        mailbox,
        _CountingPort(),
        ledger_fn=lambda: None,
        config=VerifierJudgeConfig(),
    )
    worker.start()
    task = worker._task
    assert task is not None
    worker.close()
    assert worker._task is None
    with pytest.raises(asyncio.CancelledError):
        await task
    worker.enqueue(_evidence("r1", 1))
    await asyncio.sleep(0.02)
    assert mailbox.take_ready("t1") == []


def test_judge_state_dicts_are_capped() -> None:
    mailbox = VerdictMailbox()
    worker = JudgeWorker(
        mailbox,
        _CountingPort(),
        ledger_fn=lambda: None,
        config=VerifierJudgeConfig(),
    )
    for i in range(_STATE_CAP + 1):
        worker.note_agent_tokens(f"r{i}", 1)
    assert "r0" not in worker._agent_spend
    assert f"r{_STATE_CAP}" in worker._agent_spend
    worker.close()


@pytest.mark.asyncio
async def test_signal_judge_shares_tracker_status_confidence() -> None:
    judge = SignalJudge()
    drifting = await judge.verify(
        None,
        EvidenceBundle("t1", "r1", 3, ("write_without_read",)),
    )
    assert drifting.status == "drifting"
    assert drifting.confidence == 0.6
    assert drifting.severity == "nudge"
    stuck = await judge.verify(
        None,
        EvidenceBundle("t1", "r1", 3, ("done_unmet",)),
    )
    assert stuck.status == "stuck"
    assert stuck.confidence == 0.9
