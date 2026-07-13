"""Session transcript capture for harness debugging (internal use only).

Writes one append-only NDJSON file per session under
``{workspace_root}/.monkeybot/transcripts/{session_id}.ndjson``: a manifest
record on the first write, followed by durable :class:`~monkeybot.core.runtime.events.AgentEvent`
boundaries (OpenCode V2-style), plus raw provider request/response records.

Live-only streaming deltas (``AssistantDelta``, ``ToolInputDelta``, …) are
skipped by default so the transcript is a replay-grade durable log. Set
``MONKEYBOT_TRANSCRIPT_INCLUDE_LIVE=1`` to capture every SSE event.

Not surfaced to the agent or any tool; gated by ``MONKEYBOT_TRANSCRIPT_ENABLED``
(default off) and wired only from the gateway loop (``GatewayLoopPort.start_turn``
and the provider call site in ``core/runtime/loop.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from monkeybot.core.path_safety import sanitize_path_component
from monkeybot.core.runtime.events import AgentEvent, event_to_json, is_durable_event

logger = logging.getLogger(__name__)

_TRANSCRIPT_REL_DIR = Path(".monkeybot") / "transcripts"


def transcript_enabled_from_env() -> bool:
    """Opt-in only; default off (``MONKEYBOT_TRANSCRIPT_ENABLED``)."""
    raw = os.environ.get("MONKEYBOT_TRANSCRIPT_ENABLED", "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def transcript_include_live_from_env() -> bool:
    """When true, write live-only streaming events too (default durable-only)."""
    raw = os.environ.get("MONKEYBOT_TRANSCRIPT_INCLUDE_LIVE", "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


class TranscriptWriter:
    """Append-only NDJSON transcript writer for one gateway session.

    One instance per ``session_id``; safe for concurrent turns on the same
    session via an internal lock. All file I/O runs in ``asyncio.to_thread``.
    """

    def __init__(
        self,
        session_id: str,
        *,
        workspace_root: Path,
        include_live: bool | None = None,
    ) -> None:
        self._session_id = session_id
        safe_id = sanitize_path_component(session_id)
        self._path = workspace_root.resolve() / _TRANSCRIPT_REL_DIR / f"{safe_id}.ndjson"
        self._lock = asyncio.Lock()
        self._manifest_written = False
        self._seq = 0
        self._include_live = (
            transcript_include_live_from_env() if include_live is None else include_live
        )

    @property
    def path(self) -> Path:
        return self._path

    async def ensure_manifest(self, **manifest_fields: Any) -> None:
        """Write the manifest as line 1 if this is a new transcript file (idempotent)."""
        if self._manifest_written:
            return
        async with self._lock:
            if self._manifest_written:
                return

            def _write_if_new() -> None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                if self._path.exists():
                    return
                record = {
                    "type": "SessionManifest",
                    "session_id": self._session_id,
                    "started_at": _now_iso(),
                    "durable_only": not self._include_live,
                    **manifest_fields,
                }
                line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")

            try:
                await asyncio.to_thread(_write_if_new)
            except OSError:
                logger.warning("transcript manifest write failed for %s", self._session_id, exc_info=True)
            self._manifest_written = True

    async def _append_line(self, record: dict[str, Any]) -> None:
        async with self._lock:
            self._seq += 1
            line = json.dumps({"seq": self._seq, **record}, ensure_ascii=False, separators=(",", ":"))

            def _append() -> None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")

            try:
                await asyncio.to_thread(_append)
            except OSError:
                logger.warning("transcript append failed for %s", self._session_id, exc_info=True)

    async def write_user_message(self, *, request_id: str, content: str) -> None:
        """Append the incoming user turn (not an ``AgentEvent``; harness-internal)."""
        await self._append_line(
            {"ts": _now_iso(), "type": "UserMessage", "request_id": request_id, "content": content}
        )

    async def write_event(self, event: AgentEvent) -> None:
        """Append one harness ``AgentEvent`` (durable by default; same JSON as SSE)."""
        if not self._include_live and not is_durable_event(event):
            return
        payload = json.loads(event_to_json(event))
        await self._append_line({"ts": _now_iso(), **payload})

    async def write_provider_request(
        self,
        *,
        request_id: str,
        inner_turn: int,
        model: str,
        messages: list[dict[str, object]],
        message_offset: int = 0,
        messages_reset: bool = False,
        tools: list[dict[str, object]] | None = None,
        thinking_budget: int | None,
    ) -> None:
        record: dict[str, Any] = {
            "ts": _now_iso(),
            "type": "ProviderRequest",
            "request_id": request_id,
            "inner_turn": inner_turn,
            "model": model,
            "message_offset": message_offset,
            "messages": messages,
            "thinking_budget": thinking_budget,
        }
        if messages_reset:
            record["messages_reset"] = True
        if tools is not None:
            record["tools"] = tools
        await self._append_line(record)

    async def write_provider_response(
        self,
        *,
        request_id: str,
        inner_turn: int,
        model: str,
        text: str,
        thinking: str,
        tool_requests: list[dict[str, object]],
        usage: dict[str, object],
    ) -> None:
        await self._append_line(
            {
                "ts": _now_iso(),
                "type": "ProviderResponse",
                "request_id": request_id,
                "inner_turn": inner_turn,
                "model": model,
                "text": text,
                "thinking": thinking,
                "tool_requests": tool_requests,
                "usage": usage,
            }
        )


__all__ = [
    "TranscriptWriter",
    "transcript_enabled_from_env",
    "transcript_include_live_from_env",
]
