"""Drive the live MonkeyBot v2 gateway (sessions + SSE) for eval runs."""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from models import Scenario, TurnResult

_log = logging.getLogger(__name__)


def _parse_sse_blocks(buffer: str) -> tuple[list[dict[str, Any]], str]:
    """Split SSE buffer into JSON objects (same idea as a browser SSE client)."""
    events: list[dict[str, Any]] = []
    parts = re.split(r"\r?\n\r?\n", buffer)
    rest = parts.pop() if parts else buffer
    for block in parts:
        trimmed = block.strip()
        if not trimmed or trimmed.startswith(":"):
            continue
        for line in trimmed.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return events, rest


def _event_matches_request(ev: dict[str, Any], request_id: str) -> bool:
    rid = ev.get("request_id")
    if rid is None or rid == "":
        return True
    return str(rid) == request_id


async def _collect_turn_output(
    client: httpx.AsyncClient,
    agent_url: str,
    session_id: str,
    request_id: str,
    message: str,
    *,
    timeout_sec: float = 180.0,
) -> tuple[str, str | None, str | None]:
    """Open the SSE stream first, then POST reply; read until ``TurnComplete``.

    Returns ``(assistant_text, trace_id, error_message)``.
    """
    base = agent_url.rstrip("/")
    out_parts: list[str] = []
    trace_id: str | None = None
    err_msg: str | None = None

    timeout = httpx.Timeout(timeout_sec, connect=30.0)
    async with client.stream(
        "GET",
        f"{base}/sessions/{session_id}/events",
        headers={"Accept": "text/event-stream"},
        timeout=timeout,
    ) as stream:
        stream.raise_for_status()
        reply = await client.post(
            f"{base}/sessions/{session_id}/reply",
            json={"request_id": request_id, "message": message},
            timeout=timeout,
        )
        if not reply.is_success:
            return "", None, f"reply HTTP {reply.status_code}: {reply.text[:500]}"

        buffer = ""
        async for chunk in stream.aiter_text():
            buffer += chunk
            parsed, buffer = _parse_sse_blocks(buffer)
            for ev in parsed:
                et = ev.get("type")
                if et == "AssistantDelta" and ev.get("delta"):
                    if _event_matches_request(ev, request_id):
                        out_parts.append(str(ev["delta"]))
                elif et == "Error":
                    if _event_matches_request(ev, request_id):
                        err_msg = str(ev.get("error") or "unknown error")
                        return "".join(out_parts), trace_id, err_msg
                elif et == "TurnComplete":
                    if _event_matches_request(ev, request_id):
                        tid = ev.get("trace_id")
                        trace_id = str(tid) if tid else None
                        return "".join(out_parts), trace_id, err_msg

    return "".join(out_parts), trace_id, err_msg or "SSE ended before TurnComplete"


async def run_scenario_live(
    scenario: Scenario,
    agent_url: str,
    *,
    timeout_sec: float = 180.0,
    on_turn_complete: Callable[[int, TurnResult], Awaitable[None]] | None = None,
) -> list[TurnResult]:
    """Create a session and send each user message; collect assistant output per turn."""
    base = agent_url.rstrip("/")
    timeout = httpx.Timeout(timeout_sec, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{base}/sessions", json={})
        r.raise_for_status()
        session_id = str(r.json()["session_id"])

        turns: list[TurnResult] = []
        for idx, msg in enumerate(scenario.messages):
            rid = str(uuid.uuid4())
            text, tid, err = await _collect_turn_output(
                client,
                agent_url,
                session_id,
                rid,
                msg,
                timeout_sec=timeout_sec,
            )
            if err:
                raise RuntimeError(err)
            tr = TurnResult(input=msg, output=text, trace_id=tid)
            turns.append(tr)
            if on_turn_complete is not None:
                await on_turn_complete(idx, tr)
        return turns
