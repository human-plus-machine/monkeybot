"""Tests for ``monkeybot.core.runtime.utterance_buffer``."""

from __future__ import annotations

from monkeybot.core.llm.realtime_provider import AudioFormat, RealtimeToolCall
from monkeybot.core.runtime.utterance_buffer import UtteranceBuffer

_PCM_24K = AudioFormat(encoding="pcm_s16le", sample_rate_hz=24000, channels=1, frame_ms=200)


def _chunk_for_duration_ms(duration_ms: int, fmt: AudioFormat) -> bytes:
    bytes_per_sample = 2 if fmt.encoding.startswith("pcm_s") else 1
    samples = int(duration_ms * fmt.sample_rate_hz / 1000)
    return b"\x00" * (samples * bytes_per_sample * fmt.channels)


class TestUtteranceBuffer:
    def test_empty_buffer(self) -> None:
        buf = UtteranceBuffer()
        assert not buf.in_user_turn
        assert not buf.in_assistant_turn
        assert buf.mark_user_turn_boundary().is_empty
        assert buf.mark_assistant_turn_boundary().is_empty

    def test_accumulates_user_text(self) -> None:
        buf = UtteranceBuffer()
        buf.add_user_text("hello ")
        buf.add_user_text("world")
        assert buf.in_user_turn
        utterance = buf.mark_user_turn_boundary()
        assert utterance.text == "hello world"
        assert not buf.in_user_turn

    def test_accumulates_user_audio_duration(self) -> None:
        buf = UtteranceBuffer()
        chunk = _chunk_for_duration_ms(200, _PCM_24K)
        buf.add_user_audio(chunk, fmt=_PCM_24K)
        buf.add_user_audio(chunk, fmt=_PCM_24K)
        utterance = buf.mark_user_turn_boundary()
        assert utterance.audio_duration_ms == 400

    def test_accumulates_assistant_turn(self) -> None:
        buf = UtteranceBuffer()
        buf.add_assistant_text("The answer is ")
        buf.add_assistant_text("42")
        buf.add_assistant_tool_call(RealtimeToolCall(call_id="c1", name="read_file", args={"path": "x"}))
        turn = buf.mark_assistant_turn_boundary()
        assert turn.text == "The answer is 42"
        assert len(turn.tool_calls) == 1
        assert turn.tool_calls[0].name == "read_file"
        assert not buf.in_assistant_turn

    def test_interrupt_keeps_user_input_discards_assistant(self) -> None:
        buf = UtteranceBuffer()
        buf.add_user_text("what is the")
        buf.mark_user_turn_boundary()
        buf.add_assistant_text("The answer is")
        # User barges in
        buf.add_user_text(" never mind, what about")
        buf.add_user_text(" something else")
        buf.interrupt()
        assert not buf.in_assistant_turn
        utterance = buf.mark_user_turn_boundary()
        assert utterance.text == "never mind, what about something else"

    def test_user_input_during_assistant_accumulates(self) -> None:
        buf = UtteranceBuffer()
        buf.add_assistant_text("one")
        buf.add_user_text("two")
        turn = buf.mark_assistant_turn_boundary()
        assert turn.text == "one"
        utterance = buf.mark_user_turn_boundary()
        assert utterance.text == "two"
