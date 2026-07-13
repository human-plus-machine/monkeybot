"""Test doubles for :class:`~monkeybot.core.llm.realtime_provider.RealtimeSession`."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from monkeybot.core.llm.realtime_provider import (
    AudioFormat,
    RealtimeEvent,
    RealtimeSession,
    RealtimeSessionConfig,
)


class ScriptedRealtimeSession:
    """Deterministic in-memory realtime session for unit tests."""

    def __init__(
        self,
        events: list[RealtimeEvent],
        *,
        input_format: AudioFormat | None = None,
        output_format: AudioFormat | None = None,
    ) -> None:
        self._events = list(events)
        self._input_format = input_format or AudioFormat(
            encoding="pcm_s16le", sample_rate_hz=24000, channels=1, frame_ms=200
        )
        self._output_format = output_format or AudioFormat(
            encoding="pcm_s16le", sample_rate_hz=24000, channels=1, frame_ms=200
        )
        self._audio_sent: list[bytes] = []
        self._text_sent: list[str] = []
        self._context_sent: list[str] = []
        self._tool_results_sent: list[tuple[str, str, dict[str, object], bool]] = []
        self._audio_turn_ends = 0
        self._interrupt_count = 0
        self._closed = False
        self._close_reason: str | None = None

    @property
    def input_format(self) -> AudioFormat:
        return self._input_format

    @property
    def output_format(self) -> AudioFormat:
        return self._output_format

    async def send_audio(self, chunk: bytes) -> None:
        self._audio_sent.append(chunk)

    async def end_audio_turn(self) -> None:
        self._audio_turn_ends += 1

    async def send_text(self, text: str) -> None:
        self._text_sent.append(text)

    async def send_context(self, text: str) -> None:
        self._context_sent.append(text)

    async def send_tool_results(
        self,
        results: Sequence[tuple[str, str, dict[str, object], bool]],
    ) -> None:
        self._tool_results_sent.extend(results)

    async def interrupt(self) -> None:
        self._interrupt_count += 1

    def events(self) -> AsyncIterator[RealtimeEvent]:
        async def _gen() -> AsyncIterator[RealtimeEvent]:
            for ev in list(self._events):
                yield ev

        return _gen()

    async def close(self, *, reason: str = "session_end") -> None:
        self._closed = True
        self._close_reason = reason

    def audio_sent(self) -> list[bytes]:
        return list(self._audio_sent)

    def audio_turn_ends(self) -> int:
        return self._audio_turn_ends

    def text_sent(self) -> list[str]:
        return list(self._text_sent)

    def context_sent(self) -> list[str]:
        return list(self._context_sent)

    def tool_results_sent(self) -> list[tuple[str, str, dict[str, object], bool]]:
        return list(self._tool_results_sent)

    def interrupt_count(self) -> int:
        return self._interrupt_count

    def is_closed(self) -> bool:
        return self._closed

    def close_reason(self) -> str | None:
        return self._close_reason


class ScriptedRealtimeProvider:
    """Deterministic factory for :class:`ScriptedRealtimeSession`."""

    def __init__(
        self,
        *,
        name: str = "fake-realtime",
        sessions: list[ScriptedRealtimeSession] | None = None,
    ) -> None:
        self._name = name
        self._sessions = sessions or []
        self._calls: list[tuple[str, RealtimeSessionConfig]] = []

    @property
    def name(self) -> str:
        return self._name

    async def connect(
        self,
        *,
        config: RealtimeSessionConfig,
    ) -> RealtimeSession:
        self._calls.append((config.model, config))
        if not self._sessions:
            return ScriptedRealtimeSession([])
        return self._sessions.pop(0)

    def connect_calls(self) -> list[tuple[str, RealtimeSessionConfig]]:
        return list(self._calls)


__all__ = ["ScriptedRealtimeProvider", "ScriptedRealtimeSession"]
