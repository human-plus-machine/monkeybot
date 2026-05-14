"""
SSE wire framing and mapping from agent events (kind) to SSE JSON (type).
"""

import json
from dataclasses import fields as dc_fields
from typing import Any


def format_data_event(seq: int, data: str) -> str:
    """Build one SSE data event with an `id` line (stored in replay buffer)."""
    return f"id: {seq}\ndata: {data}\n\n"


def format_ping(n: int) -> str:
    """Heartbeat comment line; not buffered for replay."""
    return f": ping {n}\n\n"


def format_active_requests(request_ids: list[str]) -> str:
    """ActiveRequests frame without an `id:` line."""
    payload = json_dumps_wire({"type": "ActiveRequests", "request_ids": request_ids})
    return f"data: {payload}\n\n"


def agent_event_to_wire_dict(event: object) -> dict[str, Any]:
    """Map AgentEvent.kind (or dict with kind) to SSE JSON with type + chat_request_id."""
    if isinstance(event, dict):
        raw = event
        kind = str(raw["kind"])
        payload = {k: v for k, v in raw.items() if k != "kind"}
    else:
        kind = str(getattr(event, "kind"))
        dcfs = getattr(event, "__dataclass_fields__", None)
        if dcfs:
            payload = {
                f.name: getattr(event, f.name)
                for f in dc_fields(event)
                if f.name != "kind"
            }
        else:
            payload = {k: v for k, v in vars(event).items() if k != "kind"}
    rid = str(payload.get("request_id", ""))
    out: dict[str, Any] = {"type": kind, "request_id": rid, "chat_request_id": rid}
    for key, val in payload.items():
        if key != "request_id":
            out[key] = val
    return out


def json_dumps_wire(obj: dict[str, Any]) -> str:
    """Stable JSON for SSE data lines (no extra whitespace)."""
    return json.dumps(obj, separators=(",", ":"))
