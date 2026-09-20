"""Request-scoped mailbox open/clear isolation (SSE and sequential Live)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from monkeybot.core.runtime.events import Error, TurnComplete, VerifierVerdict
from monkeybot.core.runtime.loop import run
from monkeybot.core.verifier.mailbox import VerdictMailbox
from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider, RecordingExecutor
from tests.core.test_loop import _ctx as loop_ctx


def _verdict(request_id: str, verdict_id: str = "v1") -> VerifierVerdict:
    return VerifierVerdict(
        request_id=request_id,
        verdict_id=verdict_id,
        checkpoint_id=f"{request_id}:1",
        status="drifting",
        severity="none",
    )


def test_put_without_open_is_dropped() -> None:
    mailbox = VerdictMailbox()
    assert mailbox.put("t1", _verdict("r1")) is False
    assert mailbox.take_ready("t1", "r1") == []
    assert mailbox.last("t1") is None


def test_late_put_cannot_reopen_a_cleared_request() -> None:
    mailbox = VerdictMailbox()
    mailbox.open_request("t1", "r1")
    mailbox.clear_request("t1", "r1")
    assert mailbox.put("t1", _verdict("r1", "late")) is False
    assert mailbox.take_ready("t1", "r1") == []
    assert mailbox.last("t1") is None
    mailbox.open_request("t1", "r2")
    assert mailbox.put("t1", _verdict("r1", "stale")) is False
    assert mailbox.put("t1", _verdict("r2", "live")) is True
    assert [v.verdict_id for v in mailbox.take_ready("t1", "r2")] == ["live"]


def test_ready_pending_and_last_are_request_scoped() -> None:
    mailbox = VerdictMailbox()
    mailbox.open_request("t1", "r1")
    mailbox.mark_pending("t1", "r1")
    mailbox.put("t1", _verdict("r1", "v1"))
    mailbox.open_request("t1", "r2")
    mailbox.put("t1", _verdict("r2", "v2"))
    assert mailbox.pending("t1", "r1") is True
    assert mailbox.pending("t1", "r2") is False
    assert [v.verdict_id for v in mailbox.take_ready("t1", "r1")] == ["v1"]
    assert [v.verdict_id for v in mailbox.take_ready("t1", "r2")] == ["v2"]
    assert mailbox.last("t1") is not None
    assert mailbox.last("t1").request_id == "r2"


def test_clear_request_drops_last_pending_and_notes() -> None:
    mailbox = VerdictMailbox()
    mailbox.open_request("t1", "r1")
    mailbox.mark_pending("t1", "r1")
    mailbox.put("t1", _verdict("r1"))
    mailbox.activate_nudge(
        "t1",
        "r1",
        replace(_verdict("r1"), severity="nudge", triggering_signals=("error_streak",)),
    )
    mailbox.put_replan("t1", "r1", "replan")
    mailbox.clear_request("t1", "r1")
    assert mailbox.last("t1") is None
    assert mailbox.pending("t1", "r1") is False
    assert mailbox.take_ready("t1", "r1") == []
    assert mailbox.peek_nudge("t1", "r1") is None
    assert mailbox.take_replan("t1", "r1") is None


def test_sequential_live_connections_do_not_leak_state() -> None:
    mailbox = VerdictMailbox()
    mailbox.open_request("sess", "rt-1")
    mailbox.mark_pending("sess", "rt-1")
    mailbox.put("sess", _verdict("rt-1", "first"))
    mailbox.activate_nudge(
        "sess",
        "rt-1",
        replace(_verdict("rt-1", "first"), severity="nudge", triggering_signals=("error_streak",)),
    )
    mailbox.clear_request("sess", "rt-1")

    assert mailbox.last("sess") is None
    assert mailbox.pending("sess") is False
    assert mailbox.take_ready("sess", "rt-1") == []
    assert mailbox.peek_nudge("sess", "rt-2") is None

    mailbox.open_request("sess", "rt-2")
    assert mailbox.put("sess", _verdict("rt-1", "late")) is False
    assert mailbox.put("sess", _verdict("rt-2", "second")) is True
    assert mailbox.last("sess") is not None
    assert mailbox.last("sess").verdict_id == "second"
    assert [v.verdict_id for v in mailbox.take_ready("sess", "rt-2")] == ["second"]


@pytest.mark.asyncio
async def test_take_ready_ignores_other_request_pending() -> None:
    import time

    from monkeybot.core.runtime.turn_loop import _take_ready

    mailbox = VerdictMailbox()
    mailbox.open_request("t1", "r1")
    mailbox.mark_pending("t1", "r1")
    started = time.monotonic()
    assert await _take_ready(mailbox, "t1", "r2", grace_s=1.0) == []
    assert time.monotonic() - started < 0.2


@pytest.mark.asyncio
async def test_take_ready_collects_all_ready_verdicts_for_the_request() -> None:
    from monkeybot.core.runtime.turn_loop import _take_ready

    mailbox = VerdictMailbox()
    mailbox.open_request("t1", "r1")
    mailbox.mark_pending("t1", "r1")
    mailbox.mark_pending("t1", "r1")
    mailbox.put("t1", _verdict("r1", "v1"))
    mailbox.put("t1", _verdict("r1", "v2"))
    mailbox.open_request("t1", "r2")
    mailbox.put("t1", _verdict("r2", "other"))
    mailbox.clear_pending("t1", "r1")
    mailbox.clear_pending("t1", "r1")
    ready = await _take_ready(mailbox, "t1", "r1", grace_s=1.0)
    assert [v.verdict_id for v in ready] == ["v1", "v2"]
    assert [v.verdict_id for v in mailbox.take_ready("t1", "r2")] == ["other"]


@pytest.mark.asyncio
async def test_tail_grace_exits_when_request_pending_reaches_zero() -> None:
    import asyncio
    import time

    from monkeybot.core.runtime.turn_loop import _take_ready

    mailbox = VerdictMailbox()
    mailbox.open_request("t1", "r1")
    mailbox.mark_pending("t1", "r1")

    async def _settle() -> None:
        await asyncio.sleep(0.04)
        mailbox.clear_pending("t1", "r1")

    asyncio.create_task(_settle())
    started = time.monotonic()
    assert await _take_ready(mailbox, "t1", "r1", grace_s=2.0) == []
    assert time.monotonic() - started < 0.4


@pytest.mark.asyncio
async def test_loop_clears_mailbox_on_error_finally() -> None:
    class BoomProvider(FakeProvider):
        async def stream(self, *args: object, **kwargs: object):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    mailbox = VerdictMailbox()
    mailbox.open_request("t1", "r1")
    mailbox.put("t1", _verdict("r1"))
    ctx = replace(loop_ctx(), verdict_mailbox=mailbox)
    events = []
    async for event in run(
        "hello",
        ctx,
        provider=BoomProvider([[]]),
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=3,
    ):
        events.append(event)
    kinds = [type(e) for e in events]
    assert kinds.index(VerifierVerdict) < kinds.index(Error)
    assert kinds.index(Error) < kinds.index(TurnComplete)
    assert mailbox.last("t1") is None
    assert mailbox.put("t1", _verdict("r1", "late")) is False
