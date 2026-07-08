"""Accumulate continuous realtime input into finalized utterances."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from monkeybot.core.llm.realtime_provider import AudioFormat, RealtimeToolCall
from monkeybot.core.types.content_blocks import ContentBlock, Text


def _chunk_duration_ms(chunk: bytes, fmt: AudioFormat) -> int:
    """Return duration of an audio chunk in milliseconds."""
    bytes_per_sample = 2 if fmt.encoding.startswith("pcm_s") else 1
    frame_size = bytes_per_sample * fmt.channels
    if frame_size == 0:
        return 0
    samples = len(chunk) // frame_size
    return int(samples * 1000 / fmt.sample_rate_hz)


def _user_text_from_content(blocks: Sequence[ContentBlock]) -> str:
    return " ".join(
        b.text.strip() for b in blocks if isinstance(b, Text) and b.text.strip()
    )


@dataclass
class FinalizedUtterance:
    """User utterance ready to commit to HistoryStore."""

    text: str
    audio_duration_ms: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.text and self.audio_duration_ms == 0


@dataclass
class AssistantTurn:
    """Assistant output accumulated during one turn."""

    text: str = ""
    tool_calls: list[RealtimeToolCall] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.text and not self.tool_calls


class UtteranceBuffer:
    """In-memory buffer for one realtime session.

    Accumulates user audio/text and assistant output. On a user turn boundary the
    finalized user utterance is returned. On an interrupt, in-flight assistant output
    is discarded while user input accumulated during the interruption is kept.
    """

    def __init__(self) -> None:
        self._user_text_parts: list[str] = []
        self._user_audio_duration_ms = 0
        self._assistant = AssistantTurn()
        self._in_user_turn = False
        self._in_assistant_turn = False
        self._last_user_text: str = ""
        self._last_assistant_turn: AssistantTurn = AssistantTurn()

    def add_user_text(self, text: str) -> None:
        """Add a partial or final user text chunk."""
        self._in_user_turn = True
        self._user_text_parts.append(text)

    def add_user_audio(self, chunk: bytes, *, fmt: AudioFormat) -> None:
        """Add a user audio chunk; tracks duration only."""
        self._in_user_turn = True
        self._user_audio_duration_ms += _chunk_duration_ms(chunk, fmt)

    def add_assistant_text(self, text: str) -> None:
        """Add a partial or final assistant text chunk."""
        self._in_assistant_turn = True
        self._assistant.text += text

    def add_assistant_tool_call(self, call: RealtimeToolCall) -> None:
        """Register a tool call emitted by the assistant during this turn."""
        self._in_assistant_turn = True
        self._assistant.tool_calls.append(call)

    def mark_user_turn_boundary(self) -> FinalizedUtterance:
        """Finalize the current user utterance and reset user state."""
        self._in_user_turn = False
        text = "".join(self._user_text_parts).strip()
        self._last_user_text = text
        duration = self._user_audio_duration_ms
        self._user_text_parts = []
        self._user_audio_duration_ms = 0
        return FinalizedUtterance(text=text, audio_duration_ms=duration)

    def mark_assistant_turn_boundary(self) -> AssistantTurn:
        """Finalize the current assistant turn and reset assistant state."""
        self._in_assistant_turn = False
        turn = AssistantTurn(
            text=self._assistant.text.strip(),
            tool_calls=list(self._assistant.tool_calls),
        )
        self._last_assistant_turn = turn
        self._assistant = AssistantTurn()
        return turn

    def interrupt(self) -> None:
        """User barge-in: keep any new user input, discard assistant output."""
        self._in_assistant_turn = False
        self._assistant = AssistantTurn()

    @property
    def in_user_turn(self) -> bool:
        return self._in_user_turn

    @property
    def in_assistant_turn(self) -> bool:
        return self._in_assistant_turn

    def assistant_tool_calls(self) -> list[RealtimeToolCall]:
        """Return tool calls accumulated during the in-flight assistant turn."""
        return list(self._assistant.tool_calls)

    def current_user_text(self) -> str:
        """Return the finalized user text, or the in-flight text if not yet finalized."""
        if self._in_user_turn:
            return "".join(self._user_text_parts).strip()
        return self._last_user_text

    def current_assistant_turn(self) -> AssistantTurn:
        """Return the last finalized assistant turn, or the in-flight one if still active."""
        if self._in_assistant_turn:
            return AssistantTurn(
                text=self._assistant.text.strip(),
                tool_calls=list(self._assistant.tool_calls),
            )
        return self._last_assistant_turn
