"""Tests for realtime session guardrails."""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from monkeybot.core.config.realtime_config import RealtimeConfig, RealtimeSessionConfig
from monkeybot.core.runtime.utterance_buffer import UtteranceBuffer
from monkeybot.gateway.realtime.errors import GuardrailError
from monkeybot.gateway.realtime.guardrails import run_guardrails
from monkeybot.gateway.realtime.session import RealtimeConnectionState

from .test_session import _FakeRealtimeSession


def _make_config(
    max_duration: int = 1800,
    idle_timeout: int = 120,
    max_response_turn: int = 300,
) -> RealtimeConfig:
    return RealtimeConfig(
        session=RealtimeSessionConfig(
            max_duration_sec=max_duration,
            idle_timeout_sec=idle_timeout,
            max_response_turn_sec=max_response_turn,
        ),
    )


def _state() -> RealtimeConnectionState:
    return RealtimeConnectionState(
        session_id="s1",
        request_id="r1",
        provider=None,  # type: ignore[arg-type]
        realtime_session=_FakeRealtimeSession(),
        buffer=UtteranceBuffer(),
        opened_at=time.monotonic(),
        last_activity_at=time.monotonic(),
    )


@pytest.mark.asyncio
async def test_max_duration_guardrail() -> None:
    state = _state()
    state.opened_at = time.monotonic() - 10
    config = _make_config(max_duration=1, idle_timeout=300)
    with pytest.raises(GuardrailError, match="max_duration"):
        await run_guardrails(state, config, poll_interval_sec=0.1)


@pytest.mark.asyncio
async def test_idle_timeout_guardrail() -> None:
    state = _state()
    state.last_activity_at = time.monotonic() - 10
    config = _make_config(idle_timeout=1)
    with pytest.raises(GuardrailError, match="idle_timeout"):
        await run_guardrails(state, config, poll_interval_sec=0.1)


@pytest.mark.asyncio
async def test_max_response_turn_guardrail() -> None:
    state = _state()
    state.transition("thinking")
    state.metrics.start_turn(time.monotonic() - 10)
    config = _make_config(max_response_turn=1)
    with pytest.raises(GuardrailError, match="max_response_turn"):
        await run_guardrails(state, config, poll_interval_sec=0.1)


@pytest.mark.asyncio
async def test_no_guardrail_when_active() -> None:
    state = _state()
    config = _make_config(max_duration=3600, idle_timeout=3600, max_response_turn=3600)
    task = asyncio.create_task(run_guardrails(state, config, poll_interval_sec=0.1))
    await asyncio.sleep(0.25)
    state.close()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
