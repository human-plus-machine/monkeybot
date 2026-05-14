"""Google Chat webhook extractor for MonkeyBot."""
from __future__ import annotations

from typing import Any


def extract_message(payload: dict[str, Any]) -> str | None:
    """Extract text from Google Chat MESSAGE event. Returns None for non-message events."""
    if payload.get("type") == "ADDED_TO_SPACE":
        return None
    return (payload.get("message") or {}).get("text")


def format_response(text: str) -> dict[str, Any]:
    """Format response as a Google Chat text message."""
    return {"text": text}


def session_id(payload: dict[str, Any]) -> str:
    """Use space name as session ID for conversation continuity."""
    return (payload.get("space") or {}).get("name") or "default"
