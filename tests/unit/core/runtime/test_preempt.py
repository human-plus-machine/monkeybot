"""race_preempt aborts the current step and lets Stop win."""

from __future__ import annotations

import asyncio

import pytest

from monkeybot.core.runtime.preempt import StepPreemptedError, race_preempt


@pytest.mark.asyncio
async def test_race_returns_when_the_work_finishes_first() -> None:
    preempt = asyncio.Event()

    async def _value() -> str:
        return "ok"

    assert await race_preempt(_value(), preempt) == "ok"
    assert preempt.is_set() is False


@pytest.mark.asyncio
async def test_race_cancels_blocked_work_when_preempt_is_set() -> None:
    preempt = asyncio.Event()
    started = asyncio.Event()

    async def _hang() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(race_preempt(_hang(), preempt))
    await started.wait()
    preempt.set()
    with pytest.raises(StepPreemptedError):
        await task


@pytest.mark.asyncio
async def test_race_stop_wins_over_preempt() -> None:
    preempt = asyncio.Event()
    cancelled = asyncio.Event()
    preempt.set()
    cancelled.set()

    async def _hang() -> None:
        await asyncio.Event().wait()

    with pytest.raises(asyncio.CancelledError):
        await race_preempt(_hang(), preempt, cancelled=cancelled)


@pytest.mark.asyncio
async def test_idle_timeout_aborts_a_wait_that_produces_nothing() -> None:
    from monkeybot.core.runtime.preempt import StreamStalledError

    async def _hang() -> None:
        await asyncio.Event().wait()

    with pytest.raises(StreamStalledError, match="no event for"):
        await race_preempt(_hang(), None, idle_timeout_s=0.05)


@pytest.mark.asyncio
async def test_a_finished_wait_beats_the_idle_timeout() -> None:
    async def _value() -> str:
        return "ok"

    assert await race_preempt(_value(), None, idle_timeout_s=1) == "ok"


@pytest.mark.asyncio
async def test_stop_beats_the_idle_timeout() -> None:
    cancelled = asyncio.Event()

    async def _hang() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(race_preempt(_hang(), None, cancelled=cancelled, idle_timeout_s=5))
    await asyncio.sleep(0.01)
    cancelled.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_steer_beats_the_idle_timeout() -> None:
    preempt = asyncio.Event()

    async def _hang() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(race_preempt(_hang(), preempt, idle_timeout_s=5))
    await asyncio.sleep(0.01)
    preempt.set()
    with pytest.raises(StepPreemptedError):
        await task


@pytest.mark.asyncio
async def test_idle_timeout_does_not_hide_a_finished_stream() -> None:
    async def _end() -> None:
        raise StopAsyncIteration

    with pytest.raises(StopAsyncIteration):
        await race_preempt(_end(), None, idle_timeout_s=1)


@pytest.mark.asyncio
async def test_scripted_hang_unblocks_when_the_stream_is_cancelled() -> None:
    from monkeybot.core.testing.mocks_provider import HangStep, ScriptedProvider

    provider = ScriptedProvider([[HangStep(timeout_s=2.0)]])
    stream = provider.stream([], [], model="fake")
    pending = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await stream.aclose()
