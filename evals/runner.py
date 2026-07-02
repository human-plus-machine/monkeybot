"""Drive the live MonkeyBot v2 gateway (sessions + SSE) for eval runs."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from models import Scenario, ToolCallRecord, TurnResult, UsageSummary

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


def _args_summary(args: dict[str, Any]) -> str:
    try:
        raw = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(args)
    return raw[:300]


async def _collect_turn_output(
    client: httpx.AsyncClient,
    agent_url: str,
    session_id: str,
    request_id: str,
    message: str,
    *,
    timeout_sec: float = 180.0,
) -> tuple[TurnResult, str | None]:
    """Open the SSE stream first, then POST reply; read until ``TurnComplete``.

    Accumulates assistant text, usage, tool-call telemetry, and summarization
    counts for the matching ``request_id``. Returns ``(turn_result, error_message)``;
    ``turn_result.output`` is empty and ``error_message`` set on failure.
    """
    base = agent_url.rstrip("/")
    out_parts: list[str] = []
    trace_id: str | None = None
    err_msg: str | None = None
    usage = UsageSummary()
    tool_calls: list[ToolCallRecord] = []
    summarizations_count = 0

    def _result() -> TurnResult:
        return TurnResult(
            input=message,
            output="".join(out_parts),
            trace_id=trace_id,
            usage=usage,
            tool_calls=tool_calls,
            summarizations_count=summarizations_count,
        )

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
            return _result(), f"reply HTTP {reply.status_code}: {reply.text[:500]}"

        buffer = ""
        async for chunk in stream.aiter_text():
            buffer += chunk
            parsed, buffer = _parse_sse_blocks(buffer)
            for ev in parsed:
                et = ev.get("type")
                if not _event_matches_request(ev, request_id):
                    continue
                if et == "AssistantDelta" and ev.get("delta"):
                    out_parts.append(str(ev["delta"]))
                elif et == "ToolCallStarted":
                    tool_calls.append(
                        ToolCallRecord(
                            tool=str(ev.get("tool") or ""),
                            args_summary=_args_summary(dict(ev.get("args") or {})),
                        )
                    )
                elif et == "ToolCallResult":
                    tool = str(ev.get("tool") or "")
                    error = ev.get("error")
                    # Match the most recent started-but-unresolved call for this tool, else append.
                    for rec in reversed(tool_calls):
                        if rec.tool == tool and rec.error is None:
                            if error:
                                rec.error = str(error)
                            break
                    else:
                        tool_calls.append(
                            ToolCallRecord(tool=tool, error=str(error) if error else None)
                        )
                elif et == "ContextSummarized":
                    summarizations_count += 1
                elif et == "Error":
                    err_msg = str(ev.get("error") or "unknown error")
                    return _result(), err_msg
                elif et == "TurnComplete":
                    tid = ev.get("trace_id")
                    trace_id = str(tid) if tid else None
                    u = ev.get("usage") or {}
                    if isinstance(u, dict):
                        usage.input_tokens = int(u.get("input_tokens", 0) or 0)
                        usage.output_tokens = int(u.get("output_tokens", 0) or 0)
                        usage.cached_tokens = int(u.get("cached_tokens", 0) or 0)
                        usage.cache_read_tokens = int(u.get("cache_read_tokens", 0) or 0)
                        usage.cache_creation_tokens = int(u.get("cache_creation_tokens", 0) or 0)
                        usage.cost_usd = float(u.get("cost_usd", 0.0) or 0.0)
                        usage.duration_ms = int(u.get("duration_ms", 0) or 0)
                    return _result(), None

    return _result(), (err_msg or "SSE ended before TurnComplete")


async def _run_session(
    client: httpx.AsyncClient,
    agent_url: str,
    messages: list[str],
    *,
    timeout_sec: float,
    start_index: int,
    on_turn_complete: Callable[[int, TurnResult], Awaitable[None]] | None,
) -> list[TurnResult]:
    """Open one session and send ``messages`` through it sequentially."""
    base = agent_url.rstrip("/")
    r = await client.post(f"{base}/sessions", json={})
    r.raise_for_status()
    session_id = str(r.json()["session_id"])

    turns: list[TurnResult] = []
    for offset, msg in enumerate(messages):
        rid = str(uuid.uuid4())
        tr, err = await _collect_turn_output(
            client, agent_url, session_id, rid, msg, timeout_sec=timeout_sec
        )
        if err:
            raise RuntimeError(err)
        turns.append(tr)
        if on_turn_complete is not None:
            await on_turn_complete(start_index + offset, tr)
    return turns


async def run_scenario_live(
    scenario: Scenario,
    agent_url: str,
    *,
    timeout_sec: float = 180.0,
    on_turn_complete: Callable[[int, TurnResult], Awaitable[None]] | None = None,
) -> list[TurnResult]:
    """Send each scenario message group through its own session; collect output per turn.

    A scenario with a single implicit session (the common case) opens one session and
    sends every message through it. A scenario with ``sessions`` set opens a fresh
    session per group, in order, against the same running agent — enough to express
    "write in session A, recall in session B" without a general orchestration DSL.
    """
    timeout = httpx.Timeout(timeout_sec, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        turns: list[TurnResult] = []
        groups = scenario.message_groups()
        for i, group in enumerate(groups):
            if i > 0 and scenario.session_pause_sec > 0:
                await asyncio.sleep(scenario.session_pause_sec)
            group_turns = await _run_session(
                client,
                agent_url,
                group,
                timeout_sec=timeout_sec,
                start_index=len(turns),
                on_turn_complete=on_turn_complete,
            )
            turns.extend(group_turns)
        return turns
