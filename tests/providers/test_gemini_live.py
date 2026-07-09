"""Tests for the Gemini Live realtime provider adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from monkeybot.core.llm.realtime_provider import (
    RealtimeAudioDelta,
    RealtimeDone,
    RealtimeError,
    RealtimeInterrupted,
    RealtimePartialTranscript,
    RealtimeTextDelta,
    RealtimeToolCall,
    RealtimeTurnBoundary,
    RealtimeUsage,
)
from monkeybot.providers.gemini_live import GeminiLiveSession

try:
    from google import genai  # noqa: F401

    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False


def _make_session() -> GeminiLiveSession:
    from monkeybot.core.llm.realtime_provider import RealtimeSessionConfig

    return GeminiLiveSession(
        config=RealtimeSessionConfig(
            model="gemini-3.1-flash-live-preview",
            system_prompt="You are a test assistant.",
            tools=[],
        ),
    )


def _mock_attr(obj: Any, **attrs: Any) -> Any:
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


def _mock_message(server_content: Any | None = None, tool_call: Any | None = None, error: Any | None = None) -> Any:
    msg: Any = type("LiveServerMessage", (), {})()
    msg.server_content = server_content
    msg.tool_call = tool_call
    msg.error = error
    return msg


def _mock_server_content(**attrs: Any) -> Any:
    sc: Any = type("LiveServerContent", (), {})()
    for key, value in attrs.items():
        setattr(sc, key, value)
    return sc


def _mock_part(text: str | None = None, inline_data: Any | None = None) -> Any:
    part: Any = type("Part", (), {})()
    part.text = text
    part.inline_data = inline_data
    return part


def _mock_blob(data: bytes, mime_type: str = "audio/pcm;rate=24000") -> Any:
    blob: Any = type("Blob", (), {})()
    blob.data = data
    blob.mime_type = mime_type
    return blob


def _mock_transcription(text: str) -> Any:
    t: Any = type("AudioTranscription", (), {})()
    t.text = text
    return t


def _mock_tool_call(*, id: str, name: str, args: Any) -> Any:
    tc: Any = type("LiveServerToolCall", (), {})()
    fc: Any = type("FunctionCall", (), {})()
    fc.id = id
    fc.name = name
    fc.args = args
    tc.function_calls = [fc]
    return tc


async def _collect(ait: AsyncIterator[Any]) -> list[Any]:
    return [item async for item in ait]


@pytest.mark.skipif(not _GENAI_AVAILABLE, reason="google-genai not installed")
class TestGeminiLiveEventMapping:
    async def test_text_delta(self) -> None:
        session = _make_session()
        msg = _mock_message(
            server_content=_mock_server_content(
                model_turn=_mock_attr(
                    type("Content", (), {})(),
                    parts=[_mock_part(text="Hello")],
                )
            )
        )
        events = await _collect(session._map_message(msg))
        assert any(isinstance(e, RealtimeTextDelta) and e.text == "Hello" for e in events)

    async def test_audio_delta(self) -> None:
        session = _make_session()
        audio = b"\x00\x01\x02\x03"
        msg = _mock_message(
            server_content=_mock_server_content(
                model_turn=_mock_attr(
                    type("Content", (), {})(),
                    parts=[_mock_part(inline_data=_mock_blob(audio))],
                )
            )
        )
        events = await _collect(session._map_message(msg))
        assert any(isinstance(e, RealtimeAudioDelta) and e.chunk == audio for e in events)

    async def test_turn_complete_emits_boundary_and_done(self) -> None:
        session = _make_session()
        msg = _mock_message(server_content=_mock_server_content(turn_complete=True))
        events = await _collect(session._map_message(msg))
        assert any(isinstance(e, RealtimeTurnBoundary) and e.role == "assistant" for e in events)
        assert any(isinstance(e, RealtimeDone) for e in events)

    async def test_interrupted(self) -> None:
        session = _make_session()
        msg = _mock_message(server_content=_mock_server_content(interrupted=True))
        events = await _collect(session._map_message(msg))
        assert any(isinstance(e, RealtimeInterrupted) for e in events)

    async def test_input_transcription(self) -> None:
        session = _make_session()
        msg = _mock_message(
            server_content=_mock_server_content(
                input_transcription=_mock_transcription("user said hello")
            )
        )
        events = await _collect(session._map_message(msg))
        assert any(
            isinstance(e, RealtimePartialTranscript)
            and e.text == "user said hello"
            and e.is_final
            for e in events
        )

    async def test_output_transcription(self) -> None:
        session = _make_session()
        msg = _mock_message(
            server_content=_mock_server_content(
                output_transcription=_mock_transcription("model said hello")
            )
        )
        events = await _collect(session._map_message(msg))
        assert any(isinstance(e, RealtimeTextDelta) and e.text == "model said hello" for e in events)

    async def test_tool_call(self) -> None:
        session = _make_session()
        msg = _mock_message(tool_call=_mock_tool_call(id="call-1", name="get_weather", args={"city": "NYC"}))
        events = await _collect(session._map_message(msg))
        assert any(
            isinstance(e, RealtimeToolCall)
            and e.call_id == "call-1"
            and e.name == "get_weather"
            and e.args == {"city": "NYC"}
            for e in events
        )
        # Tool calls without turn_complete still synthesize an assistant boundary.
        assert any(isinstance(e, RealtimeTurnBoundary) and e.role == "assistant" for e in events)
        # Tool-only messages should not emit RealtimeDone (that waits for turn_complete).
        assert not any(isinstance(e, RealtimeDone) for e in events)

    async def test_tool_call_with_json_args_string(self) -> None:
        session = _make_session()
        msg = _mock_message(
            tool_call=_mock_tool_call(id="call-2", name="search", args='{"q": "cats"}')
        )
        events = await _collect(session._map_message(msg))
        assert any(
            isinstance(e, RealtimeToolCall)
            and e.call_id == "call-2"
            and e.name == "search"
            and e.args == {"q": "cats"}
            for e in events
        )

    async def test_tool_call_with_bad_json_args(self) -> None:
        session = _make_session()
        msg = _mock_message(
            tool_call=_mock_tool_call(id="call-3", name="broken", args="not-json")
        )
        events = await _collect(session._map_message(msg))
        assert any(
            isinstance(e, RealtimeToolCall)
            and e.call_id == "call-3"
            and e.name == "broken"
            and e.parse_error is not None
            for e in events
        )

    async def test_error_message(self) -> None:
        session = _make_session()
        msg = type("LiveServerMessage", (), {})()
        msg.error = "something went wrong"
        msg.server_content = None
        msg.tool_call = None
        events = await _collect(session._map_message(msg))
        assert any(
            isinstance(e, RealtimeError)
            and "something went wrong" in e.error
            for e in events
        )

    async def test_usage_metadata(self) -> None:
        session = _make_session()
        usage = type("UsageMetadata", (), {})()
        usage.prompt_token_count = 42
        usage.candidates_token_count = 17
        msg = _mock_message(server_content=_mock_server_content(turn_complete=True))
        msg.usage_metadata = usage
        events = await _collect(session._map_message(msg))
        assert any(
            isinstance(e, RealtimeUsage)
            and e.input_tokens == 42
            and e.output_tokens == 17
            for e in events
        )


@pytest.mark.skipif(not _GENAI_AVAILABLE, reason="google-genai not installed")
class TestGeminiLiveConfigWiring:
    def test_build_live_config_honors_voice_and_max_tokens(self) -> None:
        from google.genai import types

        from monkeybot.core.llm.realtime_provider import RealtimeSessionConfig
        from monkeybot.providers.gemini_live import _build_live_config

        cfg = RealtimeSessionConfig(
            model="gemini-3.1-flash-live-preview",
            system_prompt="hi",
            tools=[],
            voice="Charon",
            max_output_tokens=256,
        )
        live = _build_live_config(cfg, types)
        voice_name = (
            live.speech_config.voice_config.prebuilt_voice_config.voice_name
        )
        assert voice_name == "Charon"
        assert live.max_output_tokens == 256

    def test_session_stores_full_config_for_connect(self) -> None:
        from monkeybot.core.llm.realtime_provider import RealtimeSessionConfig

        cfg = RealtimeSessionConfig(
            model="gemini-3.1-flash-live-preview",
            system_prompt="hi",
            tools=[],
            voice="Kore",
            max_output_tokens=128,
        )
        session = GeminiLiveSession(config=cfg)
        assert session._config.voice == "Kore"
        assert session._config.max_output_tokens == 128
