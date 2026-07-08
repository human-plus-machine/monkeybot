"""Tests for the realtime session state machine."""

from __future__ import annotations

from typing import Any

import pytest

from monkeybot.core.llm.realtime_provider import AudioFormat, RealtimeSession
from monkeybot.core.runtime.utterance_buffer import UtteranceBuffer
from monkeybot.gateway.realtime.session import (
    RealtimeConnectionState,
    SessionStateError,
)


class _FakeRealtimeSession(RealtimeSession):
    """Minimal protocol implementation for state tests."""

    def __init__(self) -> None:
        self._fmt = AudioFormat(
            encoding="pcm_s16le", sample_rate_hz=24000, channels=1, frame_ms=200
        )

    @property
    def input_format(self) -> AudioFormat:
        return self._fmt

    @property
    def output_format(self) -> AudioFormat:
        return self._fmt

    async def send_audio(self, chunk: bytes) -> None:
        pass

    async def end_audio_turn(self) -> None:
        pass

    async def send_text(self, text: str) -> None:
        pass

    async def send_context(self, text: str) -> None:
        pass

    async def send_tool_results(self, results: Any) -> None:
        pass

    async def interrupt(self) -> None:
        pass

    def events(self) -> Any:
        return iter([])

    async def close(self, *, reason: str = "session_end") -> None:
        pass


def _state() -> RealtimeConnectionState:
    return RealtimeConnectionState(
        session_id="s1",
        request_id="r1",
        provider=None,  # type: ignore[arg-type]
        realtime_session=_FakeRealtimeSession(),
        buffer=UtteranceBuffer(),
    )


def test_initial_state_is_listening() -> None:
    state = _state()
    assert state.state == "listening"
    assert state.is_idle()


def test_valid_state_transitions() -> None:
    state = _state()
    state.transition("thinking")
    state.transition("speaking")
    state.transition("interrupted")
    state.transition("listening")


def test_invalid_transition_raises() -> None:
    state = _state()
    # interrupted is not reachable directly from listening.
    with pytest.raises(SessionStateError, match="Invalid realtime transition"):
        state.transition("interrupted")


def test_listening_to_tool_running_allowed() -> None:
    """Tool-only turns (no audio/text) may go listening -> tool_running."""
    state = _state()
    state.transition("tool_running")
    assert state.state == "tool_running"


def test_tool_running_cannot_be_interrupted() -> None:
    state = _state()
    state.transition("thinking")
    state.transition("tool_running")
    # User barge-in is ignored once a tool is running (constraint 4).
    state.handle_user_interrupt()
    assert state.state == "tool_running"


def test_interrupt_while_speaking() -> None:
    state = _state()
    state.transition("thinking")
    state.transition("speaking")
    state.handle_user_interrupt()
    assert state.state == "interrupted"
    assert state.metrics.interrupt_count == 1


def test_any_state_can_close() -> None:
    state = _state()
    state.close()
    assert state.state == "closing"

    state = _state()
    state.transition("thinking")
    state.close()
    assert state.state == "closing"

    state = _state()
    state.transition("thinking")
    state.transition("speaking")
    state.close()
    assert state.state == "closing"

    state = _state()
    state.transition("thinking")
    state.transition("interrupted")
    state.close()
    assert state.state == "closing"

    state = _state()
    state.transition("thinking")
    state.transition("tool_running")
    state.close()
    assert state.state == "closing"
