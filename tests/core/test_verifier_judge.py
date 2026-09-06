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
