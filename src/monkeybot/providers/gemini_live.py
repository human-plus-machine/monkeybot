"""Gemini Live realtime provider (Vertex AI Agent Engine / google-genai).

Install with the ``realtime-gemini`` extra:

    uv sync --extra realtime-gemini

This adapter maps the google-genai live duplex API to the harness's
:class:`~monkeybot.core.llm.realtime_provider.RealtimeProvider` protocol.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.llm.realtime_provider import (
    AudioFormat,
    RealtimeAudioDelta,
    RealtimeDone,
    RealtimeError,
    RealtimeEvent,
    RealtimeInterrupted,
    RealtimePartialTranscript,
    RealtimeProvider,
    RealtimeSession,
    RealtimeSessionConfig,
    RealtimeTextDelta,
    RealtimeToolCall,
    RealtimeTurnBoundary,
    RealtimeUsage,
)
from monkeybot.core.types.interfaces import LLMError
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers.gemini import (
    _normalize_vertex_model,
    _tool_defs_to_declarations,
    _vertex_project_and_location,
)

_log = logging.getLogger(__name__)


# Gemini Live currently requires 24kHz PCM s16le mono for output.
# Input is resampled by the service; we send 24kHz to match the gateway contract.
_GEMINI_LIVE_INPUT_FORMAT = AudioFormat(
    encoding="pcm_s16le", sample_rate_hz=24000, channels=1, frame_ms=200
)
_GEMINI_LIVE_OUTPUT_FORMAT = AudioFormat(
    encoding="pcm_s16le", sample_rate_hz=24000, channels=1, frame_ms=200
)


def _require_genai() -> tuple[Any, Any]:
    """Import google-genai and return (genai, types)."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise LLMError(
            "google-genai is required for GeminiLiveProvider. "
            "Install with: uv sync --extra realtime-gemini"
        ) from exc
    return genai, types


def _build_live_config(
    config: RealtimeSessionConfig,
    types: Any,
) -> Any:
    """Build a ``LiveConnectConfig`` from the harness realtime config."""
    # Gemini Live preview models (e.g. gemini-3.1-flash-live-preview) do not support the
    # combined AUDIO+TEXT modality. Use AUDIO by default so the agent responds through
    # voice; text transcripts are still available via output_audio_transcription.
    voice_name = config.voice or "Puck"
    cfg_kwargs: dict[str, Any] = {
        "response_modalities": ["AUDIO"],
        # Keep post-tool turns on the audio path. Some Live SDK versions only honor
        # modalities via generation_config after send_tool_response.
        "generation_config": types.GenerationConfig(response_modalities=["AUDIO"]),
        # Explicit speech_config keeps post-tool turns on the audio path. Without it,
        # some Live models fall back to text-only after send_tool_response.
        "speech_config": types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        ),
    }

    if config.system_prompt:
        cfg_kwargs["system_instruction"] = types.Content(
            parts=[types.Part(text=config.system_prompt)]
        )

    if config.tools:
        decls = _tool_defs_to_declarations(config.tools)
        cfg_kwargs["tools"] = [types.Tool(function_declarations=decls)]

    # Transcriptions are useful for both text and audio modes.
    try:
        cfg_kwargs["input_audio_transcription"] = types.AudioTranscriptionConfig()
        cfg_kwargs["output_audio_transcription"] = types.AudioTranscriptionConfig()
    except Exception:
        _log.debug("google-genai version does not support audio transcription config")

    if config.max_output_tokens is not None:
        try:
            cfg_kwargs["max_output_tokens"] = config.max_output_tokens
        except Exception:
            _log.warning("ignoring unsupported max_output_tokens config")

    return types.LiveConnectConfig(**cfg_kwargs)


class GeminiLiveSession(RealtimeSession):
    """One open Gemini Live duplex session."""

    def __init__(
        self,
        *,
        config: RealtimeSessionConfig,
        api_key: str | None = None,
    ) -> None:
        self._config = config
        self._model = config.model
        self._system_prompt = config.system_prompt
        self._tools = list(config.tools)
        self._api_key = api_key
        # Gemini Live currently requires 24kHz PCM s16le mono for I/O.
        self._input_format = _GEMINI_LIVE_INPUT_FORMAT
        self._output_format = _GEMINI_LIVE_OUTPUT_FORMAT
        self._session: Any | None = None
        self._session_cm: Any | None = None

    @property
    def input_format(self) -> AudioFormat:
        return self._input_format

    @property
    def output_format(self) -> AudioFormat:
        return self._output_format

    async def _connect(self) -> Any:
        """Enter the Gemini Live session context manager and return the SDK session."""
        if self._session is not None:
            return self._session

        genai, types = _require_genai()
        model_param = _normalize_vertex_model(self._model)

        if self._api_key:
            client = genai.Client(api_key=self._api_key)
        else:
            project, location = _vertex_project_and_location(model_param)
            client = genai.Client(vertexai=True, project=project, location=location)

        # Pass the full harness config so voice / max_output_tokens are honored.
        live_config = _build_live_config(self._config, types)

        cm = client.aio.live.connect(model=model_param, config=live_config)
        self._session_cm = cm
        try:
            self._session = await cm.__aenter__()
        except Exception as exc:
            self._session_cm = None
            raise LLMError(f"Failed to connect to Gemini Live: {exc}") from exc
        return self._session

    async def send_audio(self, chunk: bytes) -> None:
        session = await self._connect()
        from google.genai import types

        await session.send_realtime_input(
            audio=types.Blob(
                data=chunk,
                mime_type=f"audio/pcm;rate={self._input_format.sample_rate_hz}",
            )
        )

    async def end_audio_turn(self) -> None:
        """Tell Gemini Live the user stopped speaking so it can respond immediately.

        Without this, Gemini waits on its own VAD silence timeout after the last
        audio chunk — which is why typed turns feel fast and spoken turns feel slow.
        """
        session = await self._connect()
        await session.send_realtime_input(audio_stream_end=True)

    async def send_text(self, text: str) -> None:
        session = await self._connect()
        from google.genai import types

        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]),
            turn_complete=True,
        )

    async def send_context(self, text: str) -> None:
        """Inject tool result/context into the live session as a user turn.

        Gemini Live does not have a dedicated "context" role; the closest equivalent is a
        turn-complete client content message. This is semantically a user/system message,
        which is acceptable because the harness already committed the actual tool result to
        HistoryStore. The live session only needs the compact summary to continue.
        """
        session = await self._connect()
        from google.genai import types

        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]),
            turn_complete=True,
        )

    async def send_tool_results(
        self,
        results: Sequence[tuple[str, str, dict[str, object], bool]],
    ) -> None:
        """Return tool results via Gemini Live's dedicated ``send_tool_response`` RPC.

        Gemini Live will not continue the conversation until function responses are
        returned for outstanding tool calls. Sending them as plain text via
        ``send_context`` leaves the session hung waiting for tool responses.
        """
        if not results:
            return
        session = await self._connect()
        from google.genai import types

        # INTERRUPT tells Gemini to speak the follow-up immediately in the configured
        # AUDIO modality. Default/WHEN_IDLE scheduling often yields text-only turns
        # after tools, which is why the CLI showed transcripts with no audio.
        function_responses = [
            types.FunctionResponse(
                id=call_id or None,
                name=name,
                response=payload if not is_error else {"error": payload.get("error", payload)},
                scheduling=types.FunctionResponseScheduling.INTERRUPT,
            )
            for call_id, name, payload, is_error in results
        ]
        await session.send_tool_response(function_responses=function_responses)

    async def interrupt(self) -> None:
        """Gemini Live has no explicit interrupt RPC; interruption is implicit when the
        client sends new audio/text. We send an empty client content turn-complete so the
        server cancels any in-flight generation and emits ``interrupted=True``.
        """
        session = await self._connect()
        try:
            await session.send_client_content(
                turns=[],
                turn_complete=True,
            )
        except Exception as exc:
            _log.warning("Gemini Live interrupt signal failed: %s", exc)

    def events(self) -> AsyncIterator[RealtimeEvent]:
        async def _gen() -> AsyncIterator[RealtimeEvent]:
            session = await self._connect()
            try:
                # Gemini Live's receive() yields one assistant turn. Loop it so the session
                # stays open across multiple user/assistant turns.
                while True:
                    async for msg in session.receive():
                        async for event in self._map_message(msg):
                            yield event
            except Exception as exc:
                _log.exception("Gemini Live receive loop failed")
                yield RealtimeError(error=f"Gemini Live receive loop failed: {exc}")

        return _gen()

    async def _map_message(self, msg: Any) -> AsyncIterator[RealtimeEvent]:
        """Map one ``LiveServerMessage`` to zero or more harness ``RealtimeEvent``s."""
        if getattr(msg, "error", None):
            yield RealtimeError(error=str(msg.error))
            return

        turn_complete = False
        server_content = getattr(msg, "server_content", None)
        if server_content is not None:
            interrupted = bool(getattr(server_content, "interrupted", False))
            if interrupted:
                yield RealtimeInterrupted()

            input_transcription = getattr(server_content, "input_transcription", None)
            if input_transcription:
                text = getattr(input_transcription, "text", "")
                if text:
                    yield RealtimePartialTranscript(text=text, is_final=True)

            output_transcription = getattr(server_content, "output_transcription", None)
            if output_transcription:
                text = getattr(output_transcription, "text", "")
                if text:
                    yield RealtimeTextDelta(text=text)

            model_turn = getattr(server_content, "model_turn", None)
            if model_turn:
                parts = getattr(model_turn, "parts", []) or []
                for part in parts:
                    if part is None:
                        continue
                    text = getattr(part, "text", None)
                    if text:
                        yield RealtimeTextDelta(text=text)
                    inline_data = getattr(part, "inline_data", None)
                    if inline_data is not None:
                        data = getattr(inline_data, "data", b"")
                        if data:
                            yield RealtimeAudioDelta(chunk=data)

            turn_complete = bool(getattr(server_content, "turn_complete", False))

        usage_metadata = getattr(msg, "usage_metadata", None)
        if usage_metadata is not None:
            yield RealtimeUsage(
                input_tokens=int(
                    getattr(usage_metadata, "prompt_token_count", 0)
                    or getattr(usage_metadata, "input_token_count", 0)
                    or 0
                ),
                output_tokens=int(
                    getattr(usage_metadata, "candidates_token_count", 0)
                    or getattr(usage_metadata, "response_token_count", 0)
                    or getattr(usage_metadata, "output_token_count", 0)
                    or 0
                ),
            )

        # Emit tool calls before any turn boundary so the gateway buffers them first.
        had_tool_calls = False
        tool_call = getattr(msg, "tool_call", None)
        if tool_call is not None:
            function_calls = getattr(tool_call, "function_calls", []) or []
            for call in function_calls:
                had_tool_calls = True
                call_id = str(getattr(call, "id", "") or "")
                name = str(getattr(call, "name", "") or "")
                args_text = getattr(call, "args", None)
                if args_text is None:
                    args_text = getattr(call, "function_call", None)
                if isinstance(args_text, str):
                    try:
                        args = json.loads(args_text)
                    except json.JSONDecodeError:
                        yield RealtimeToolCall(
                            call_id=call_id,
                            name=name,
                            args={},
                            parse_error=f"Failed to parse tool args: {args_text}",
                        )
                        continue
                elif isinstance(args_text, dict):
                    args = dict(args_text)
                else:
                    args = {}
                yield RealtimeToolCall(call_id=call_id, name=name, args=args)

        # Gemini Live often emits tool calls without turn_complete. Synthesize an
        # assistant turn boundary so the gateway dispatches tools immediately.
        if turn_complete or had_tool_calls:
            yield RealtimeTurnBoundary(role="assistant")
            if turn_complete:
                yield RealtimeDone()

    async def close(self, *, reason: str = "session_end") -> None:
        _ = reason
        if self._session is None:
            return
        try:
            if self._session_cm is not None:
                await self._session_cm.__aexit__(None, None, None)
        except Exception:
            _log.exception("Gemini Live session close failed")
        finally:
            self._session = None
            self._session_cm = None


class GeminiLiveProvider(RealtimeProvider):
    """Gemini Live realtime provider factory."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "gemini-live"

    async def connect(self, *, config: RealtimeSessionConfig) -> RealtimeSession:
        session = GeminiLiveSession(
            config=config,
            api_key=self._api_key,
        )
        await session._connect()
        return session
