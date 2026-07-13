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

    def test_final_transcript_after_audio_boundary(self) -> None:
        buf = UtteranceBuffer()
        chunk = _chunk_for_duration_ms(200, _PCM_24K)
        buf.add_user_audio(chunk, fmt=_PCM_24K)
        utterance = buf.mark_user_turn_boundary()
        assert utterance.text == ""
        assert utterance.audio_duration_ms == 200
        buf.apply_final_user_transcript("hello from speech")
        assert buf.current_user_text() == "hello from speech"
        assert buf.consume_user_text_for_commit() == "hello from speech"
        # Second assistant boundary must not re-commit the same utterance.
        assert buf.consume_user_text_for_commit() == ""

    def test_consume_user_text_for_commit_is_once_per_utterance(self) -> None:
        buf = UtteranceBuffer()
        buf.add_user_text("read the file")
        buf.mark_user_turn_boundary()
        assert buf.consume_user_text_for_commit() == "read the file"
        assert buf.consume_user_text_for_commit() == ""
        buf.add_user_text("next turn")
        buf.mark_user_turn_boundary()
        assert buf.consume_user_text_for_commit() == "next turn"

    def test_typed_text_tool_then_prose_commits_user_once(self) -> None:
        """Typed turns must finalize before assistant tool/prose boundaries.

        Without mark_user_turn_boundary(), in_user_turn stays true and
        consume_user_text_for_commit() re-returns the same text on every
        assistant boundary (tool-call then follow-up prose).
        """
        buf = UtteranceBuffer()
        buf.add_user_text("list files in workspace")
        buf.mark_user_turn_boundary()
        assert not buf.in_user_turn

        buf.add_assistant_tool_call(
            RealtimeToolCall(call_id="c1", name="list_dir", args={"path": "."})
        )
        tool_turn = buf.mark_assistant_turn_boundary()
        assert len(tool_turn.tool_calls) == 1
        assert buf.consume_user_text_for_commit() == "list files in workspace"

        buf.add_assistant_text("Here are the files.")
        prose_turn = buf.mark_assistant_turn_boundary()
        assert prose_turn.text == "Here are the files."
        # Second boundary must not re-commit the typed user utterance.
        assert buf.consume_user_text_for_commit() == ""

    def test_empty_consume_allows_late_transcript(self) -> None:
        buf = UtteranceBuffer()
        chunk = _chunk_for_duration_ms(200, _PCM_24K)
        buf.add_user_audio(chunk, fmt=_PCM_24K)
        buf.mark_user_turn_boundary()
        assert buf.consume_user_text_for_commit() == ""
        buf.apply_final_user_transcript("late transcript")
        assert buf.consume_user_text_for_commit() == "late transcript"
        assert buf.consume_user_text_for_commit() == ""
