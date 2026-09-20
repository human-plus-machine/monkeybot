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


def test_stale_verdict_does_not_activate_after_recovery() -> None:
    mailbox = VerdictMailbox()
    mailbox.open_request("t1", "r1")
    mailbox.set_current_signals("t1", "r1", [])
    verdict = VerifierVerdict(
        request_id="r1",
        verdict_id="v-stale",
        checkpoint_id="r1:3",
        status="stuck",
        severity="nudge",
        rationale="error_streak",
        triggering_signals=("error_streak",),
    )
    assert mailbox.activate_nudge("t1", "r1", verdict) is False
    assert mailbox.peek_nudge("t1", "r1") is None


def test_stale_verdict_does_not_activate_for_new_signal_episode() -> None:
    mailbox = VerdictMailbox()
    mailbox.open_request("t1", "r1")
    mailbox.set_current_signals("t1", "r1", ["error_streak"])
    original_epochs = mailbox.signal_epochs("t1", "r1", ["error_streak"])
    verdict = VerifierVerdict(
        request_id="r1",
        verdict_id="v-old-episode",
        checkpoint_id="r1:3",
        status="stuck",
        severity="nudge",
        rationale="error_streak",
        triggering_signals=("error_streak",),
        triggering_signal_epochs=original_epochs,
    )

    mailbox.set_current_signals("t1", "r1", [])
    mailbox.set_current_signals("t1", "r1", ["error_streak"])

    assert mailbox.signal_epochs("t1", "r1", ["error_streak"]) != original_epochs
    assert mailbox.activate_nudge("t1", "r1", verdict) is False
    assert mailbox.peek_nudge("t1", "r1") is None


def test_out_of_order_verdict_deposit_drops_older_checkpoint() -> None:
    mailbox = VerdictMailbox()
    mailbox.open_request("t1", "r1")
    newer = VerifierVerdict(
        request_id="r1",
        verdict_id="new",
        checkpoint_id="r1:4",
        status="on_track",
        severity="none",
    )
    older = VerifierVerdict(
        request_id="r1",
        verdict_id="old",
        checkpoint_id="r1:1",
        status="stuck",
        severity="nudge",
        triggering_signals=("error_streak",),
    )

    assert mailbox.put("t1", newer) is True
    assert mailbox.put("t1", older) is False
    assert [verdict.verdict_id for verdict in mailbox.take_ready("t1", "r1")] == ["new"]
    assert mailbox.last("t1") is newer


def test_epoch_less_verdict_keeps_only_live_signals() -> None:
    mailbox = VerdictMailbox()
    mailbox.open_request("t1", "r1")
    mailbox.set_current_signals("t1", "r1", ["error_streak", "write_without_read"])
    mailbox.set_current_signals("t1", "r1", ["write_without_read"])
    verdict = VerifierVerdict(
        request_id="r1",
        verdict_id="v-epoch-less",
        checkpoint_id="r1:3",
        status="stuck",
        severity="nudge",
        triggering_signals=("error_streak", "write_without_read"),
    )
    assert mailbox.activate_nudge("t1", "r1", verdict) is True
    note = mailbox.peek_nudge("t1", "r1")
    assert note is not None
    from monkeybot.core.verifier.intervention import SIGNAL_INSTRUCTIONS

    assert SIGNAL_INSTRUCTIONS["write_without_read"] in note
    assert SIGNAL_INSTRUCTIONS["error_streak"] not in note


def test_same_turn_recovery_replaces_nudge_in_last() -> None:
    mailbox = VerdictMailbox()
    mailbox.open_request("t1", "r1")
    nudge = VerifierVerdict(
        request_id="r1",
        verdict_id="nudge",
        checkpoint_id="r1:3",
        status="drifting",
        severity="nudge",
        triggering_signals=("error_streak",),
    )
    recovery = VerifierVerdict(
        request_id="r1",
        verdict_id="recovered",
        checkpoint_id="r1:3",
        status="on_track",
        severity="none",
    )
    assert mailbox.put("t1", nudge) is True
    assert mailbox.put("t1", recovery) is True
    assert mailbox.last("t1") is recovery


def test_unparseable_checkpoint_does_not_overwrite_numeric() -> None:
    mailbox = VerdictMailbox()
    mailbox.open_request("t1", "r1")
    numeric = VerifierVerdict(
        request_id="r1",
        verdict_id="numeric",
        checkpoint_id="r1:4",
        status="drifting",
        severity="nudge",
    )
    empty = VerifierVerdict(
        request_id="r1",
        verdict_id="empty",
        checkpoint_id="",
        status="stuck",
        severity="block",
    )
    assert mailbox.put("t1", numeric) is True
    assert mailbox.put("t1", empty) is False
    assert mailbox.last("t1") is numeric
    mailbox.clear_request("t1", "r1")
    mailbox.open_request("t1", "r1")
    assert mailbox.put("t1", empty) is True
    assert mailbox.put("t1", numeric) is True
    assert mailbox.last("t1") is numeric
