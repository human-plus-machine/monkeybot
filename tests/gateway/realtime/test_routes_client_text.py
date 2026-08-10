"""Regression: ClientTextFrame must finalize the user turn."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from monkeybot.core.llm.realtime_provider import (
    AudioFormat,
    RealtimeSession,
    RealtimeTextDelta,
    RealtimeToolCall,
    RealtimeTurnBoundary,
)
from monkeybot.core.runtime.events import TurnComplete
from monkeybot.core.runtime.utterance_buffer import UtteranceBuffer
from monkeybot.gateway.realtime.routes import (
    _handle_assistant_boundary,
    _handle_client_frames,
    _handle_provider_event,
)
from monkeybot.gateway.realtime.session import RealtimeConnectionState


class _FakeRealtimeSession(RealtimeSession):
    def __init__(self) -> None:
        self._fmt = AudioFormat(
            encoding="pcm_s16le", sample_rate_hz=24000, channels=1, frame_ms=200
        )
        self.sent_text: list[str] = []
        self.sent_context: list[str] = []

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
        self.sent_text.append(text)

    async def send_context(self, text: str) -> None:
        self.sent_context.append(text)

    async def send_tool_results(self, results: Any) -> None:
        pass

    async def interrupt(self) -> None:
        pass

    def events(self) -> Any:
        return iter([])

    async def close(self, *, reason: str = "session_end") -> None:
        pass


def _state(session: _FakeRealtimeSession | None = None) -> RealtimeConnectionState:
    rt = session or _FakeRealtimeSession()
    return RealtimeConnectionState(
        session_id="s1",
        request_id="r1",
        provider=None,  # type: ignore[arg-type]
        realtime_session=rt,
        buffer=UtteranceBuffer(),
    )


@pytest.mark.asyncio
async def test_client_text_frame_finalizes_user_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Typed input must call mark_user_turn_boundary so idle flush can run."""
    session = _FakeRealtimeSession()
    state = _state(session)
    state.enqueue_idle_delivery("hook after typed turn")

    async def _fake_receive(_ws: Any) -> Any:
        yield '{"kind":"text","text":"list files"}'

    flushed: list[Any] = []

    async def _fake_flush(s: RealtimeConnectionState) -> None:
        while s.is_idle() and not s.idle_delivery_queue.empty():
            flushed.append(s.idle_delivery_queue.get_nowait())

    monkeypatch.setattr(
        "monkeybot.gateway.realtime.routes._receive_frames",
        _fake_receive,
    )
    monkeypatch.setattr(
        "monkeybot.gateway.realtime.routes._flush_idle_deliveries",
        _fake_flush,
    )

    await _handle_client_frames(MagicMock(), state, session.input_format)

    assert session.sent_text == ["list files"]
    assert not state.buffer.in_user_turn
    assert state.is_idle()
    assert flushed == ["hook after typed turn"]
    assert state.buffer.consume_user_text_for_commit() == "list files"
    # Reset commit flag simulation: second consume after tool/prose must be empty.
    # Re-seed as if first assistant boundary already consumed.
    assert state.buffer.consume_user_text_for_commit() == ""


@pytest.mark.asyncio
async def test_typed_text_tool_then_prose_commits_user_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typed text → tool boundary → prose must not re-commit the user row."""
    session = _FakeRealtimeSession()
    state = _state(session)

    async def _fake_receive(_ws: Any) -> Any:
        yield '{"kind":"text","text":"read README"}'

    monkeypatch.setattr(
        "monkeybot.gateway.realtime.routes._receive_frames",
        _fake_receive,
    )
    monkeypatch.setattr(
        "monkeybot.gateway.realtime.routes._flush_idle_deliveries",
        AsyncMock(),
    )

    await _handle_client_frames(MagicMock(), state, session.input_format)
    assert not state.buffer.in_user_turn

    # Avoid full run_realtime_turn; assert buffer commit semantics across boundaries.
    boundary_calls: list[str] = []

    async def _fake_assistant_boundary(*_args: Any, **_kwargs: Any) -> None:
        boundary_calls.append(state.buffer.consume_user_text_for_commit())

    monkeypatch.setattr(
        "monkeybot.gateway.realtime.routes._handle_assistant_boundary",
        _fake_assistant_boundary,
    )
    monkeypatch.setattr(
        "monkeybot.gateway.realtime.routes._send_frame",
        AsyncMock(),
    )

    ws = MagicMock()
    ctx = MagicMock()
    history = MagicMock()
    deps = MagicMock()

    await _handle_provider_event(
        ws,
        state,
        RealtimeToolCall(call_id="c1", name="read_file", args={"path": "README"}),
        ctx,
        history,
        deps,
        None,
        None,
    )
    await _handle_provider_event(
        ws,
        state,
        RealtimeTurnBoundary(role="assistant"),
        ctx,
        history,
        deps,
        None,
        None,
    )
    assert boundary_calls == ["read README"]

    # Provider continues with prose after tools.
    if state.state == "tool_running":
        state.transition("listening")
    await _handle_provider_event(
        ws,
        state,
        RealtimeTextDelta(text="Here is README."),
        ctx,
        history,
        deps,
        None,
        None,
    )
    await _handle_provider_event(
        ws,
        state,
        RealtimeTurnBoundary(role="assistant"),
        ctx,
        history,
        deps,
        None,
        None,
    )
    assert boundary_calls == ["read README", ""]


@pytest.mark.asyncio
async def test_assistant_boundary_passes_transcript_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    state.buffer.add_user_text("hello")
    state.buffer.mark_user_turn_boundary()
    state.buffer.add_assistant_text("hi")
    state.buffer.mark_assistant_turn_boundary()
    writer = MagicMock()
    writer.write_event = AsyncMock()
    state.transcript_writer = writer
    seen: dict[str, Any] = {}

    async def _fake_run(*_args: Any, **kwargs: Any) -> Any:
        seen["writer"] = kwargs["transcript_writer"]
        yield TurnComplete(request_id="r1")

    monkeypatch.setattr(
        "monkeybot.gateway.realtime.routes._create_tool_executor",
        lambda *_args, **_kwargs: MagicMock(),
    )
    monkeypatch.setattr("monkeybot.gateway.realtime.routes.run_realtime_turn", _fake_run)

    await _handle_assistant_boundary(
        MagicMock(), state, MagicMock(), MagicMock(), MagicMock(), None, None
    )

    assert seen["writer"] is writer
    writer.write_event.assert_awaited_once()
