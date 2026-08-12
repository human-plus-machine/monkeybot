"""Deterministic memory drawer / outbox identifiers."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone


def outbox_id(*, agent_id: str, thread_id: str, message_id: str, role: str) -> str:
    """Stable outbox and drawer id for one persisted history message."""
    payload = f"{agent_id}\0{thread_id}\0{message_id}\0{role}".encode()
    return "turn_" + hashlib.sha256(payload).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conversation_wing(workspace_id: str | None) -> str:
    value = (workspace_id or "").strip()
    return value or "main"
