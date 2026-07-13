"""Realtime WebSocket gateway for MonkeyBot."""

from __future__ import annotations

from monkeybot.gateway.realtime.app import create_realtime_app
from monkeybot.gateway.realtime.deps import RealtimeDependencies
from monkeybot.gateway.realtime.errors import RealtimeError
from monkeybot.gateway.realtime.guardrails import run_guardrails
from monkeybot.gateway.realtime.manager import RealtimeSessionManager
from monkeybot.gateway.realtime.metrics import RealtimeMetrics
from monkeybot.gateway.realtime.routes import create_realtime_router
from monkeybot.gateway.realtime.session import RealtimeConnectionState
from monkeybot.gateway.realtime.wire import (
    ServerConnectedFrame,
    encode_server_frame,
    parse_client_frame,
)

__all__ = [
    "RealtimeConnectionState",
    "RealtimeDependencies",
    "RealtimeError",
    "RealtimeMetrics",
    "RealtimeSessionManager",
    "ServerConnectedFrame",
    "create_realtime_app",
    "create_realtime_router",
    "encode_server_frame",
    "parse_client_frame",
    "run_guardrails",
]
