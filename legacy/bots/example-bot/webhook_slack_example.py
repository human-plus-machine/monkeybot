"""Slack webhook extractor reference for MonkeyBot."""
from __future__ import annotations

from typing import Any


def extract_message(payload: dict[str, Any]) -> str | None:
    """Extract text from Slack Events API payload. Returns None for bot messages."""
    event = payload.get("event") or {}
    if event.get("subtype") == "bot_message":
        return None
    return event.get("text")


def format_response(text: str) -> dict[str, Any]:
    """Format response as a Slack message dict."""
    return {"text": text}


def session_id(payload: dict[str, Any]) -> str:
    """Use Slack channel as session ID."""
    event = payload.get("event") or {}
    return str(event.get("channel") or "default")
