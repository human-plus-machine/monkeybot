"""Tests for ``monkeybot.core.testing.mocks_realtime_provider``."""

from __future__ import annotations

from monkeybot.core.llm.realtime_provider import (
    RealtimeDone,
    RealtimePartialTranscript,
    RealtimeTurnBoundary,
)
from monkeybot.core.testing.mocks_realtime_provider import (
    ScriptedRealtimeProvider,
    ScriptedRealtimeSession,
)


async def _collect_events(session: ScriptedRealtimeSession) -> list[object]:
    return [ev async for ev in session.events()]


class TestScriptedRealtimeSession:
    async def test_yields_programmed_events(self) -> None:
        session = ScriptedRealtimeSession(
            events=[
                RealtimePartialTranscript(text="hello", is_final=False),
                RealtimePartialTranscript(text="hello world", is_final=True),
                RealtimeTurnBoundary(role="user"),
                RealtimeDone(),
            ]
        )
        events = await _collect_events(session)
        assert len(events) == 4
        assert events[0].text == "hello"
        assert events[1].is_final is True
        assert events[2].role == "user"
        assert events[3].kind == "RealtimeDone"

    async def test_records_audio_and_text(self) -> None:
        session = ScriptedRealtimeSession([])
        await session.send_audio(b"chunk1")
        await session.send_audio(b"chunk2")
        await session.send_text("hello")
        await session.send_context("tool result")
        await session.interrupt()
        await session.interrupt()
        await session.close(reason="test")

        assert session.audio_sent() == [b"chunk1", b"chunk2"]
        assert session.text_sent() == ["hello"]
        assert session.context_sent() == ["tool result"]
        assert session.interrupt_count() == 2
        assert session.is_closed() is True
        assert session.close_reason() == "test"

    async def test_default_audio_formats(self) -> None:
        session = ScriptedRealtimeSession([])
        assert session.input_format.encoding == "pcm_s16le"
        assert session.input_format.sample_rate_hz == 24000
        assert session.output_format.channels == 1


class TestScriptedRealtimeProvider:
    async def test_connect_returns_session_and_records_call(self) -> None:
        expected = ScriptedRealtimeSession([])
        provider = ScriptedRealtimeProvider(sessions=[expected])
        from monkeybot.core.llm.realtime_provider import RealtimeSessionConfig
        from monkeybot.core.types.types_tools import ToolDef

        config = RealtimeSessionConfig(
            model="gemini-2.5-flash",
            system_prompt="test",
            tools=[ToolDef(name="read_file", description="x", input_schema={})],
        )
        session = await provider.connect(config=config)
        assert session is expected
        assert len(provider.connect_calls()) == 1
        assert provider.connect_calls()[0][0] == "gemini-2.5-flash"

    async def test_connect_returns_empty_session_when_none_left(self) -> None:
        provider = ScriptedRealtimeProvider()
        from monkeybot.core.llm.realtime_provider import RealtimeSessionConfig

        config = RealtimeSessionConfig(model="x", system_prompt="", tools=[])
        session = await provider.connect(config=config)
        assert isinstance(session, ScriptedRealtimeSession)
        assert await _collect_events(session) == []
