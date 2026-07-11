"""Encode client realtime frames to JSON for the WebSocket wire."""

from __future__ import annotations

import json
from typing import Any

from monkeybot.gateway.realtime.wire import (
    ClientCloseFrame,
    ClientElicitationResponseFrame,
    ClientTextFrame,
    ClientToolConfirmationResponseFrame,
)


def encode_client_frame(frame: Any) -> str:
    """Encode a client control frame to JSON text."""
    payload: dict[str, Any] = {"kind": frame.kind}
    if isinstance(frame, ClientCloseFrame):
        payload["reason"] = frame.reason
    elif isinstance(frame, ClientTextFrame):
        payload["text"] = frame.text
    elif isinstance(frame, ClientToolConfirmationResponseFrame):
        payload.update(
            {
                "tool_call_id": frame.tool_call_id,
                "approved": frame.approved,
                "reason": frame.reason,
            }
        )
    elif isinstance(frame, ClientElicitationResponseFrame):
        payload.update(
            {
                "elicitation_id": frame.elicitation_id,
                "user_data": frame.user_data,
                "cancelled": frame.cancelled,
            }
        )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
