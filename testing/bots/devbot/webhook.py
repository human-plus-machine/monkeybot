"""Simple HTTP webhook for DevBot.

Accepts JSON POST with {"message": "...", "user_id": "..."}.
Works with curl, the bench suite, and any generic HTTP client.
"""
from __future__ import annotations

from typing import Any


def extract_message(payload: dict[str, Any]) -> str | None:
    return payload.get("message") or None


def format_response(text: str) -> dict[str, Any]:
    return {"reply": text}


def session_id(payload: dict[str, Any]) -> str:
    return str(payload.get("user_id") or "default")
