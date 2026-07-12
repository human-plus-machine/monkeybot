"""Frame → ChatUiEvent mapping for RealtimeSessionController."""

from __future__ import annotations

import asyncio
import json

from monkeybot.gateway.realtime.wire import (
    ClientTextFrame,
    ServerConnectedFrame,
    ServerErrorFrame,
    ServerInterruptedFrame,
    ServerSessionEndedFrame,
    ServerTextDeltaFrame,
    ServerToolCallFrame,
    ServerTurnBoundaryFrame,
    encode_server_frame,
)

from monkeybot_cli.chat_session import ChatUiEvent
from monkeybot_cli.realtime.wire_encode import encode_client_frame
from monkeybot_cli.realtime.session_controller import RealtimeSessionController


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_text_delta_emits_assistant_events() -> None:
    events: list[ChatUiEvent] = []
    ctrl = RealtimeSessionController(
        gateway_url="ws://127.0.0.1:8080",
        session_id="s1",
        emit=events.append,
    )

    async def _body() -> None:
        await ctrl._handle_server_frame(
            encode_server_frame(ServerTextDeltaFrame(delta="Hi", is_final=False))
        )
        await ctrl._handle_server_frame(
            encode_server_frame(ServerTextDeltaFrame(delta="!", is_final=True))
        )

    _run(_body())
    kinds = [e.kind for e in events]
    assert kinds == ["assistant_start", "assistant_delta", "assistant_delta", "turn_complete"]
    assert events[1].payload["delta"] == "Hi"
    assert events[2].payload["delta"] == "!"


def test_connected_emits_session_ready() -> None:
    events: list[ChatUiEvent] = []
    ctrl = RealtimeSessionController(
        gateway_url="ws://127.0.0.1:8080",
        session_id="s1",
        emit=events.append,
    )

    async def _body() -> None:
        await ctrl._handle_server_frame(
            encode_server_frame(
                ServerConnectedFrame(
                    session_id="s1",
                    input_format="pcm_s16le_24khz_mono",
                    output_format="pcm_s16le_24khz_mono",
                    chunk_ms=200,
                )
            )
        )

    _run(_body())
    assert events[0].kind == "session_ready"
    assert events[0].payload["session_id"] == "s1"


def test_tool_call_interrupted_error_ended() -> None:
    events: list[ChatUiEvent] = []
    ctrl = RealtimeSessionController(
        gateway_url="ws://127.0.0.1:8080",
        session_id="s1",
        emit=events.append,
    )

    async def _body() -> None:
        await ctrl._handle_server_frame(
            encode_server_frame(
                ServerToolCallFrame(call_id="c1", name="read_file", args={"path": "a"})
            )
        )
        await ctrl._handle_server_frame(encode_server_frame(ServerInterruptedFrame()))
        await ctrl._handle_server_frame(encode_server_frame(ServerErrorFrame(error="boom")))
        await ctrl._handle_server_frame(
            encode_server_frame(ServerSessionEndedFrame(reason="client_close"))
        )

    _run(_body())
    kinds = [e.kind for e in events if e.kind != "voice_state"]
    assert kinds == ["tool_started", "turn_aborted", "turn_error", "stream_ended"]
    assert events[0].payload["call_id"] == "c1"
    assert events[2].payload["error"] == "boom" if events[2].kind == "turn_error" else True
    assert any(e.kind == "turn_error" and e.payload["error"] == "boom" for e in events)


def test_assistant_turn_boundary_closes_open_assistant() -> None:
    events: list[ChatUiEvent] = []
    ctrl = RealtimeSessionController(
        gateway_url="ws://127.0.0.1:8080",
        session_id="s1",
        emit=events.append,
    )

    async def _body() -> None:
        await ctrl._handle_server_frame(
            encode_server_frame(ServerTextDeltaFrame(delta="x", is_final=False))
        )
        await ctrl._handle_server_frame(
            encode_server_frame(ServerTurnBoundaryFrame(role="assistant"))
        )

    _run(_body())
    kinds = [e.kind for e in events]
    assert "assistant_start" in kinds
    assert kinds[-1] == "turn_complete"


def test_encode_client_text_still_importable() -> None:
    raw = json.loads(encode_client_frame(ClientTextFrame(text="hi")))
    assert raw == {"kind": "text", "text": "hi"}


def test_tool_result_and_user_transcript_and_usage_map() -> None:
    from monkeybot.gateway.realtime.wire import (
        ServerToolResultFrame,
        ServerUsageFrame,
        ServerUserTranscriptFrame,
    )

    events: list[ChatUiEvent] = []
    ctrl = RealtimeSessionController(
        gateway_url="ws://127.0.0.1:8080",
        session_id="s1",
        emit=events.append,
    )

    async def _body() -> None:
        await ctrl._handle_server_frame(
            encode_server_frame(
                ServerToolResultFrame(
                    call_id="c1", name="read_file", result="data", error=None
                )
            )
        )
        await ctrl._handle_server_frame(
            encode_server_frame(ServerUserTranscriptFrame(text="hi there", is_final=True))
        )
        await ctrl._handle_server_frame(
            encode_server_frame(
                ServerUsageFrame(
                    usage={
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "context_window_tokens": 1000,
                    }
                )
            )
        )

    _run(_body())
    kinds = [e.kind for e in events]
    assert kinds == ["tool_finished", "user_transcript", "usage_updated"]
    assert events[0].payload["call_id"] == "c1"
    assert events[1].payload["text"] == "hi there"
    assert events[2].payload["usage"].input_tokens == 10


def test_set_ptt_held_emits_voice_state() -> None:
    events: list[ChatUiEvent] = []
    ctrl = RealtimeSessionController(
        gateway_url="ws://127.0.0.1:8080",
        session_id="s1",
        emit=events.append,
        audio_enabled=True,
    )
    ctrl.set_ptt_held(True)
    assert any(e.kind == "voice_state" for e in events)


def test_hitl_confirmation_maps_to_hitl_required() -> None:
    from monkeybot.gateway.realtime.wire import ServerToolConfirmationFrame

    events: list[ChatUiEvent] = []
    ctrl = RealtimeSessionController(
        gateway_url="ws://127.0.0.1:8080",
        session_id="s1",
        emit=events.append,
    )

    async def _body() -> None:
        await ctrl._handle_server_frame(
            encode_server_frame(
                ServerToolConfirmationFrame(
                    tool_call_id="c9",
                    tool_name="shell",
                    prompt="Allow shell?",
                    arguments={"cmd": "ls"},
                    timeout_sec=300.0,
                )
            )
        )

    _run(_body())
    assert events[-1].kind == "hitl_required"
    assert events[-1].payload["tool_call_id"] == "c9"
    assert events[-1].payload["timeout_sec"] == 300.0


def test_abort_and_hitl_retain_background_tasks() -> None:
    """abort_turn / provide_hitl_answer must keep task refs (not fire-and-forget)."""
    from monkeybot.gateway.realtime.wire import ServerToolConfirmationFrame
    from monkeybot_cli.chat_session import HitlAnswer

    sent: list[str] = []

    class FakeWs:
        async def send(self, raw: str) -> None:
            sent.append(raw)
            await asyncio.sleep(0)

    events: list[ChatUiEvent] = []
    ctrl = RealtimeSessionController(
        gateway_url="ws://127.0.0.1:8080",
        session_id="s1",
        emit=events.append,
    )
    ctrl._ws = FakeWs()
    ctrl._stream_alive = True

    async def _body() -> None:
        await ctrl._handle_server_frame(
            encode_server_frame(
                ServerToolConfirmationFrame(
                    tool_call_id="c1",
                    tool_name="shell",
                    prompt="Allow?",
                    arguments={},
                    timeout_sec=120.0,
                )
            )
        )
        ctrl.abort_turn()
        assert ctrl._background_tasks, "abort_turn must retain the interrupt task"
        await asyncio.sleep(0)
        # Wait for abort task to finish, then exercise HITL send retention.
        while ctrl._background_tasks:
            await asyncio.sleep(0)
        ctrl._pending_hitl = {
            "hitl_kind": "confirm",
            "tool_call_id": "c1",
            "tool_name": "shell",
            "prompt": "Allow?",
            "arguments": {},
            "timeout_sec": 120.0,
        }
        ctrl.provide_hitl_answer(HitlAnswer(approved=True, text="y"))
        assert ctrl._background_tasks, "provide_hitl_answer must retain the send task"
        while ctrl._background_tasks:
            await asyncio.sleep(0)

    _run(_body())
    assert any("interrupt" in s for s in sent)
    assert any("tool_confirmation_response" in s for s in sent)


def test_hitl_elicitation_maps_timeout_sec() -> None:
    from monkeybot.gateway.realtime.wire import ServerElicitationFrame

    events: list[ChatUiEvent] = []
    ctrl = RealtimeSessionController(
        gateway_url="ws://127.0.0.1:8080",
        session_id="s1",
        emit=events.append,
    )

    async def _body() -> None:
        await ctrl._handle_server_frame(
            encode_server_frame(
                ServerElicitationFrame(
                    elicitation_id="e1",
                    prompt="Name?",
                    schema={"type": "object"},
                    timeout_sec=90.0,
                )
            )
        )

    _run(_body())
    assert events[-1].kind == "hitl_required"
    assert events[-1].payload["hitl_kind"] == "elicit"
    assert events[-1].payload["elicitation_id"] == "e1"
    assert events[-1].payload["timeout_sec"] == 90.0
