"""Realtime error taxonomy.

Every error in the realtime loop is either (a) surfaced to the client as a typed
``RealtimeError`` event and the session is closed, or (b) logged and treated as
non-fatal (e.g. a malformed client frame). Close/send failures must be logged, never
silently discarded.
"""

from __future__ import annotations


class RealtimeError(Exception):
    """Base class for all realtime gateway errors."""

    client_visible: bool = True
    close_code: int = 1011
    log_level: str = "error"

    def __init__(self, message: str, *, details: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ProviderConnectionError(RealtimeError):
    """Failed to establish the provider realtime session."""

    close_code = 1011


class ProviderStreamError(RealtimeError):
    """The provider stream failed mid-session."""

    close_code = 1011


class ClientProtocolError(RealtimeError):
    """Malformed or unexpected client frame."""

    close_code = 1008
    log_level = "warning"


class AudioFormatError(RealtimeError):
    """Client and gateway audio formats do not match."""

    close_code = 1008


class SubagentDispatchError(RealtimeError):
    """A subagent/task dispatch failed. Non-fatal; surfaced to the client but session continues."""

    client_visible = True
    close_code = 0  # do not close the WebSocket
    log_level = "warning"


class GatewayInternalError(RealtimeError):
    """Unexpected gateway internal error."""

    close_code = 1011


class GuardrailError(RealtimeError):
    """Session closed due to a guardrail trigger (idle, max duration, etc.)."""

    close_code = 1001


class ConcurrencyLimitError(RealtimeError):
    """Realtime concurrency limit reached; upgrade rejected."""

    close_code = 1013  # Try Again Later
    log_level = "warning"


class SessionConflictError(RealtimeError):
    """A live WebSocket already exists for this session_id."""

    close_code = 1008
    log_level = "warning"
