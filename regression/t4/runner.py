"""Drive the live monkeybot v2 gateway (sessions + SSE) for eval runs."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from regression.schema.steps import DueSteps, Step
from regression.t4.gateway import Gateway
from regression.t4.models import (
    Scenario,
    ScenarioRun,
    ToolCallRecord,
    TurnResult,
    UsageSummary,
    VerdictRecord,
)

_log = logging.getLogger(__name__)

# Evals have no human. A model that stops to ask would otherwise wait forever, so every
# ask_user prompt gets this answer and the agent must decide or report on its own.
NO_HUMAN_ANSWER = (
    "No one is available to answer questions. Use your best judgment, do not wait for input, "
    "and finish the task, or report clearly why you cannot."
)


def _parse_sse_blocks(buffer: str) -> tuple[list[dict[str, Any]], str]:
    """Split SSE buffer into JSON objects (same idea as a browser SSE client).

    A block's ``id:`` line is stored as ``_sse_id`` so a resume can name the last
    frame read. Callers that only look at ``type`` are unchanged.
    """
    events: list[dict[str, Any]] = []
    parts = re.split(r"\r?\n\r?\n", buffer)
    rest = parts.pop() if parts else buffer
    for block in parts:
        trimmed = block.strip()
        if not trimmed or trimmed.startswith(":"):
            continue
        sse_id: str | None = None
        for line in trimmed.splitlines():
            if line.startswith("id:"):
                sse_id = line[3:].strip() or None
                continue
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                _log.debug("dropping malformed SSE data line: %r", raw[:200])
                continue
            if isinstance(payload, dict):
                if sse_id:
                    payload = dict(payload)
                    payload["_sse_id"] = sse_id
                events.append(payload)
    return events, rest


def _frame_parts(event: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """The event payload, and the SSE id when the block had one."""
    sse_id = event.get("_sse_id")
    clean = {key: value for key, value in event.items() if key != "_sse_id"}
    frame = dict(clean)
    if isinstance(sse_id, str) and sse_id:
        frame["id"] = sse_id
        return clean, sse_id
    return clean, None


# After the first ``TurnComplete`` the stream is read this long for a second one with the same
# request id (T4-GW-01). Long enough for a duplicate emitted by the same turn, short next to a
# model call.
DUPLICATE_TURN_COMPLETE_GRACE_SEC = 0.3


def _event_matches_request(ev: dict[str, Any], request_id: str) -> bool:
    rid = ev.get("request_id")
    if rid is None or rid == "":
        return True
    return str(rid) == request_id


# Mirrors core_tool_executor aliases: path/file_path/file for read/write/replace,
# root (and grep's path alias) for glob/grep, plus Codex patch headers.
_PATH_ARG_KEYS = ("path", "file_path", "file", "paths", "root")
_PATCH_PATH_RE = re.compile(
    r"^\*\*\* (?:Add File|Delete File|Update File|Move to):\s*(.+)$",
    re.MULTILINE,
)


def _path_args(args: dict[str, Any]) -> list[str]:
    """Extract path-like tool args for ``files_not_touched`` glob matching."""
    out: list[str] = []
    for key in _PATH_ARG_KEYS:
        raw = args.get(key)
        if isinstance(raw, str) and raw:
            out.append(raw)
        elif isinstance(raw, list):
            out.extend(str(p) for p in raw if p)
    for key in ("patch_text", "patch"):
        raw = args.get(key)
        if isinstance(raw, str) and raw:
            out.extend(match.strip() for match in _PATCH_PATH_RE.findall(raw) if match.strip())
    return out


def _result_ok(error: object, result: object) -> bool:
    """Outcome of one tool call: a tool error, or a JSON result carrying ``ok: false``, is a failure."""
    if error:
        return False
    if isinstance(result, str) and result.lstrip().startswith("{"):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return True
        if isinstance(parsed, dict) and isinstance(parsed.get("ok"), bool):
            return parsed["ok"]
    return True


def _verdict_from_event(ev: dict[str, Any]) -> VerdictRecord:
    signals = ev.get("triggering_signals") or []
    if not isinstance(signals, list):
        signals = [str(signals)]
    return VerdictRecord(
        status=str(ev.get("status") or ""),
        severity=str(ev.get("severity") or ""),
        triggering_signals=[str(s) for s in signals],
    )


def _args_summary(args: dict[str, Any]) -> str:
    try:
        raw = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(args)
    return raw[:300]


async def _extra_turn_completes(
    chunks: AsyncIterator[str], buffer: str, request_id: str, grace_sec: float
) -> int:
    """Count further ``TurnComplete`` events for ``request_id`` seen within ``grace_sec``."""
    extra = 0
    deadline = time.monotonic() + grace_sec
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            chunk = await asyncio.wait_for(chunks.__anext__(), remaining)
        except (TimeoutError, StopAsyncIteration):
            break
        buffer += chunk
        parsed, buffer = _parse_sse_blocks(buffer)
        extra += sum(
            1
            for ev in parsed
            if ev.get("type") == "TurnComplete" and _event_matches_request(ev, request_id)
        )
    return extra


class _TurnState:
    """Mutable accumulators for one request, including a drop-and-resume."""

    def __init__(self, message: str, session_id: str, request_id: str, session_index: int) -> None:
        self.message = message
        self.session_id = session_id
        self.request_id = request_id
        self.session_index = session_index
        self.out_parts: list[str] = []
        self.trace_id: str | None = None
        self.usage = UsageSummary()
        self.tool_calls: list[ToolCallRecord] = []
        self.summarizations_count = 0
        self.verdicts: list[VerdictRecord] = []
        self.turn_completes = 0
        self.events: list[dict[str, Any]] = []
        self.frames: list[dict[str, Any]] = []
        self.last_seen_id: str | None = None

    def result(self) -> TurnResult:
        return TurnResult(
            input=self.message,
            output="".join(self.out_parts),
            trace_id=self.trace_id,
            usage=self.usage,
            tool_calls=self.tool_calls,
            summarizations_count=self.summarizations_count,
            verdicts=self.verdicts,
            turn_completes=self.turn_completes,
            session_id=self.session_id,
            request_id=self.request_id,
            session_index=self.session_index,
            events=self.events,
            frames=self.frames,
        )


class _Consume:
    def __init__(self) -> None:
        self.done = False
        self.drop = False
        self.resume_id: str | None = None
        self.error: str | None = None


def _absorb_event(state: _TurnState, event: dict[str, Any]) -> str | None:
    """Fold one matching event into ``state``. Returns an error to stop on."""
    kind = event.get("type")
    if state.turn_completes:
        if kind == "TurnComplete":
            state.turn_completes += 1
        return None
    if kind == "AssistantDelta" and event.get("delta"):
        state.out_parts.append(str(event["delta"]))
    elif kind == "ToolCallStarted":
        args = dict(event.get("args") or {})
        state.tool_calls.append(
            ToolCallRecord(
                tool=str(event.get("tool") or ""),
                args_summary=_args_summary(args),
                path_args=_path_args(args),
                call_id=str(event.get("call_id") or ""),
                args=args,
            )
        )
    elif kind == "ToolCallResult":
        tool = str(event.get("tool") or "")
        error = event.get("error")
        ok = _result_ok(error, event.get("result"))
        call_id = str(event.get("call_id") or "")
        for rec in reversed(state.tool_calls):
            if rec.ok is not None or rec.tool != tool:
                continue
            if call_id and rec.call_id and rec.call_id != call_id:
                continue
            if error:
                rec.error = str(error)
            rec.ok = ok
            if isinstance(event.get("result"), str):
                rec.result = event["result"]
            break
        else:
            state.tool_calls.append(
                ToolCallRecord(
                    tool=tool,
                    error=str(error) if error else None,
                    call_id=call_id,
                    ok=ok,
                )
            )
    elif kind == "ContextSummarized":
        state.summarizations_count += 1
    elif kind == "VerifierVerdict":
        state.verdicts.append(_verdict_from_event(event))
    elif kind == "Error":
        return str(event.get("error") or "unknown error")
    elif kind == "TurnComplete":
        tid = event.get("trace_id")
        state.trace_id = str(tid) if tid else None
        usage = event.get("usage") or {}
        if isinstance(usage, dict):
            state.usage.input_tokens = int(usage.get("input_tokens", 0) or 0)
            state.usage.output_tokens = int(usage.get("output_tokens", 0) or 0)
            state.usage.cached_tokens = int(usage.get("cached_tokens", 0) or 0)
            state.usage.cache_read_tokens = int(usage.get("cache_read_tokens", 0) or 0)
            state.usage.cache_creation_tokens = int(usage.get("cache_creation_tokens", 0) or 0)
            state.usage.cost_usd = float(usage.get("cost_usd", 0.0) or 0.0)
            state.usage.duration_ms = int(usage.get("duration_ms", 0) or 0)
        state.turn_completes = 1
    return None


async def _confirm_tool(
    client: httpx.AsyncClient,
    base: str,
    session_id: str,
    step: Step,
    event: dict[str, Any],
    timeout: httpx.Timeout,
) -> None:
    call_id = step.call_id or event.get("tool_call_id") or event.get("call_id")
    if not call_id:
        raise RuntimeError("confirm_tool needs a tool_call_id on the confirmation event")
    approved = step.decision != "deny"
    response = await client.post(
        f"{base}/sessions/{session_id}/tool-confirmations/{call_id}",
        json={"approved": approved, "always": step.decision == "always", "reason": step.reason},
        timeout=timeout,
    )
    if not response.is_success:
        raise RuntimeError(f"tool confirmation HTTP {response.status_code}: {response.text[:500]}")


async def _reload_config(
    client: httpx.AsyncClient,
    base: str,
    step: Step,
    gateway: Gateway | None,
    timeout: httpx.Timeout,
) -> None:
    if gateway is None:
        raise RuntimeError("reload_config needs the gateway the runner started for this scenario")
    gateway.rewrite_config(step.config or {})
    response = await client.post(f"{base}/admin/config/reload", json={}, timeout=timeout)
    if not response.is_success:
        raise RuntimeError(f"config reload HTTP {response.status_code}: {response.text[:500]}")


async def _apply_steps(
    client: httpx.AsyncClient,
    base: str,
    session_id: str,
    steps: list[Step],
    event: dict[str, Any],
    sse_id: str | None,
    gateway: Gateway | None,
    timeout: httpx.Timeout,
) -> _Consume:
    effect = _Consume()
    for step in steps:
        if step.action == "confirm_tool":
            await _confirm_tool(client, base, session_id, step, event, timeout)
        elif step.action == "reload_config":
            await _reload_config(client, base, step, gateway, timeout)
        elif step.action == "disconnect_sse":
            effect.drop = True
        elif step.action == "reconnect_sse":
            effect.resume_id = sse_id
        else:
            raise RuntimeError(f"live runner cannot run {step.action} in the middle of a turn")
    return effect


async def _consume_stream(
    stream: httpx.Response,
    state: _TurnState,
    *,
    client: httpx.AsyncClient,
    base: str,
    due: DueSteps | None,
    gateway: Gateway | None,
    timeout: httpx.Timeout,
    duplicate_grace_sec: float,
) -> _Consume:
    """Read one SSE connection until the turn finishes or a step drops it."""
    outcome = _Consume()
    buffer = ""
    chunks = stream.aiter_text()
    async for chunk in chunks:
        buffer += chunk
        parsed, buffer = _parse_sse_blocks(buffer)
        for raw in parsed:
            event, sse_id = _frame_parts(raw)
            if sse_id:
                state.last_seen_id = sse_id
            if not _event_matches_request(event, state.request_id):
                continue
            if not event.get("request_id"):
                event = dict(event)
                event["request_id"] = state.request_id
            state.events.append(event)
            frame = dict(event)
            if sse_id:
                frame["id"] = sse_id
            state.frames.append(frame)
            if state.turn_completes:
                if event.get("type") == "TurnComplete":
                    state.turn_completes += 1
                continue
            if event.get("type") == "AskUserRequest" and event.get("tool_call_id"):
                answer = await client.post(
                    f"{base}/sessions/{state.session_id}/ask-user/{event['tool_call_id']}",
                    json={"answer": NO_HUMAN_ANSWER},
                    timeout=timeout,
                )
                if not answer.is_success:
                    _log.warning("ask_user auto-answer failed: HTTP %s", answer.status_code)
            error = _absorb_event(state, event)
            if error:
                outcome.error = error
                outcome.done = True
                return outcome
            if due is not None:
                effect = await _apply_steps(
                    client,
                    base,
                    state.session_id,
                    due.take(event),
                    event,
                    sse_id,
                    gateway,
                    timeout,
                )
                if effect.drop:
                    outcome.drop = True
                    outcome.resume_id = effect.resume_id
                    return outcome
        if state.turn_completes:
            state.turn_completes += await _extra_turn_completes(
                chunks, buffer, state.request_id, duplicate_grace_sec
            )
            outcome.done = True
            return outcome
    return outcome


async def _collect_turn_output(
    client: httpx.AsyncClient,
    agent_url: str,
    session_id: str,
    request_id: str,
    message: str,
    *,
    timeout_sec: float = 180.0,
    duplicate_grace_sec: float = DUPLICATE_TURN_COMPLETE_GRACE_SEC,
    session_index: int = 1,
    due: DueSteps | None = None,
    gateway: Gateway | None = None,
    post: str = "reply",
) -> tuple[TurnResult, str | None]:
    """Open the SSE stream first, then POST the turn; read until ``TurnComplete``.

    ``post`` is ``reply`` (returns before the turn runs) or ``invoke_tick`` (blocks
    until the turn finishes, so that POST runs while the stream is read). A
    ``disconnect_sse`` step closes the stream; ``reconnect_sse`` opens it again
    with ``Last-Event-ID`` set to the last frame id read. After the first
    ``TurnComplete`` the stream is read for ``duplicate_grace_sec`` more so a
    second one shows up in ``turn_completes`` (T4-GW-01).
    """
    base = agent_url.rstrip("/")
    state = _TurnState(message, session_id, request_id, session_index)
    timeout = httpx.Timeout(timeout_sec, connect=30.0)
    resume_id: str | None = None
    posted = False
    post_task: asyncio.Task[httpx.Response] | None = None
    while True:
        headers = {"Accept": "text/event-stream"}
        if resume_id:
            headers["Last-Event-ID"] = resume_id
        async with client.stream(
            "GET",
            f"{base}/sessions/{session_id}/events",
            headers=headers,
            timeout=timeout,
        ) as stream:
            stream.raise_for_status()
            if not posted:
                if post == "invoke_tick":
                    post_task = asyncio.create_task(
                        client.post(
                            f"{base}/scheduler/invoke-tick",
                            json={
                                "session_id": session_id,
                                "request_id": request_id,
                                "message": message,
                            },
                            timeout=timeout,
                        )
                    )
                else:
                    reply = await client.post(
                        f"{base}/sessions/{session_id}/reply",
                        json={"request_id": request_id, "message": message},
                        timeout=timeout,
                    )
                    if not reply.is_success:
                        return state.result(), (
                            f"reply HTTP {reply.status_code}: {reply.text[:500]}"
                        )
                posted = True
            outcome = await _consume_stream(
                stream,
                state,
                client=client,
                base=base,
                due=due,
                gateway=gateway,
                timeout=timeout,
                duplicate_grace_sec=duplicate_grace_sec,
            )
        if post_task is not None and (outcome.done or outcome.error or not outcome.drop):
            response = await post_task
            post_task = None
            if not response.is_success and outcome.error is None:
                outcome.error = f"invoke-tick HTTP {response.status_code}: {response.text[:500]}"
        if outcome.error:
            return state.result(), outcome.error
        if outcome.done:
            return state.result(), None
        if outcome.drop:
            if not outcome.resume_id:
                return state.result(), (
                    "reconnect_sse needs the id of the last frame read before the drop"
                )
            resume_id = outcome.resume_id
            continue
        break
    _log.warning(
        "SSE ended before TurnComplete session=%s request=%s partial_output_chars=%d",
        session_id,
        request_id,
        sum(len(part) for part in state.out_parts),
    )
    if post_task is not None:
        response = await post_task
        if not response.is_success:
            return state.result(), (
                f"invoke-tick HTTP {response.status_code}: {response.text[:500]}"
            )
    return state.result(), "SSE ended before TurnComplete"


async def _run_session(
    client: httpx.AsyncClient,
    agent_url: str,
    messages: list[str],
    *,
    timeout_sec: float,
    start_index: int,
    session_index: int,
    due: DueSteps | None,
    gateway: Gateway | None,
    on_turn_complete: Callable[[int, TurnResult], Awaitable[None]] | None,
) -> list[TurnResult]:
    """Open one session and send ``messages`` through it sequentially."""
    base = agent_url.rstrip("/")
    response = await client.post(f"{base}/sessions", json={})
    response.raise_for_status()
    session_id = str(response.json()["session_id"])
    _log.info("eval session opened session=%s messages=%d", session_id, len(messages))

    turns: list[TurnResult] = []
    for offset, msg in enumerate(messages):
        request_id = str(uuid.uuid4())
        turn, err = await _collect_turn_output(
            client,
            agent_url,
            session_id,
            request_id,
            msg,
            timeout_sec=timeout_sec,
            session_index=session_index,
            due=due,
            gateway=gateway,
        )
        if err:
            raise RuntimeError(err)
        _log.info(
            "turn complete session=%s turn=%d duration_ms=%d tool_calls=%d",
            session_id,
            start_index + offset,
            turn.usage.duration_ms,
            len(turn.tool_calls),
        )
        turns.append(turn)
        if on_turn_complete is not None:
            await on_turn_complete(start_index + offset, turn)
    return turns


def _tick_prompt(row: dict[str, Any]) -> str:
    """The user message the scheduler would post for this loop row."""
    from monkeybot.core.persistence.scheduled_loops import ScheduledLoopRow, format_tick_prompt

    mapped = ScheduledLoopRow(
        loop_id=str(row["loop_id"]),
        session_id=str(row["session_id"]),
        status=str(row.get("status") or "active"),
        prompt=str(row.get("prompt") or ""),
        interval_ms=int(row.get("interval_ms") or 0),
        max_ticks=row.get("max_ticks"),
        max_runtime_ms=row.get("max_runtime_ms"),
        skip_if_busy=bool(row.get("skip_if_busy", True)),
        tick_index=int(row.get("tick_index") or 0),
        next_tick_at_ms=int(row.get("next_tick_at_ms") or 0),
        started_at_ms=int(row.get("started_at_ms") or 0),
        last_tick_at_ms=row.get("last_tick_at_ms"),
        last_error=row.get("last_error") if isinstance(row.get("last_error"), str) else None,
        stop_reason=row.get("stop_reason") if isinstance(row.get("stop_reason"), str) else None,
        tick_in_flight=bool(row.get("tick_in_flight", False)),
        kind=str(row.get("kind") or "loop"),
    )
    return format_tick_prompt(mapped)


async def _create_loop(
    client: httpx.AsyncClient, base: str, step: Step, timeout: httpx.Timeout
) -> tuple[str, str, str]:
    opened = await client.post(f"{base}/sessions", json={}, timeout=timeout)
    opened.raise_for_status()
    session_id = str(opened.json()["session_id"])
    body: dict[str, Any] = {
        "prompt": step.prompt,
        "interval": step.interval,
        "session_id": session_id,
        "confirmed": True,
        "skip_if_busy": True,
    }
    if step.max_ticks is not None:
        body["max_ticks"] = step.max_ticks
    if step.max_runtime:
        body["max_runtime"] = step.max_runtime
    if step.max_ticks is None and not step.max_runtime:
        body["unbounded"] = True
    created = await client.post(f"{base}/scheduler/loops", json=body, timeout=timeout)
    if not created.is_success:
        raise RuntimeError(f"create loop HTTP {created.status_code}: {created.text[:500]}")
    row = created.json().get("loop")
    if not isinstance(row, dict) or not row.get("loop_id"):
        raise RuntimeError("create loop returned no loop row")
    return session_id, str(row["loop_id"]), _tick_prompt(row)


async def _read_history(
    client: httpx.AsyncClient, base: str, session_id: str, timeout: httpx.Timeout
) -> list[dict[str, Any]]:
    response = await client.get(f"{base}/api/chat-history/{session_id}", timeout=timeout)
    if not response.is_success:
        raise RuntimeError(f"chat history HTTP {response.status_code}: {response.text[:500]}")
    payload = response.json()
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        raise RuntimeError("chat history returned no messages list")
    return [row for row in messages if isinstance(row, dict)]


async def _stop_loop(
    client: httpx.AsyncClient, base: str, loop_id: str, timeout: httpx.Timeout
) -> None:
    response = await client.post(f"{base}/scheduler/loops/{loop_id}/stop", timeout=timeout)
    if not response.is_success:
        _log.warning("stop loop %s HTTP %s", loop_id, response.status_code)


async def _run_setup(
    client: httpx.AsyncClient,
    agent_url: str,
    scenario: Scenario,
    produced: ScenarioRun,
    gateway: Gateway | None,
    timeout_sec: float,
    on_turn_complete: Callable[[int, TurnResult], Awaitable[None]] | None,
) -> list[str]:
    """Steps with no ``when``: create a loop, invoke one tick, read history."""
    base = agent_url.rstrip("/")
    timeout = httpx.Timeout(timeout_sec, connect=30.0)
    loop_session: str | None = None
    tick_prompt: str | None = None
    loop_ids: list[str] = []
    for step in scenario.steps:
        if step.when is not None:
            continue
        if step.action == "create_loop":
            loop_session, loop_id, tick_prompt = await _create_loop(client, base, step, timeout)
            loop_ids.append(loop_id)
        elif step.action == "invoke_tick":
            if loop_session is None or tick_prompt is None:
                raise RuntimeError("invoke_tick needs a preceding create_loop")
            request_id = str(uuid.uuid4())
            turn, err = await _collect_turn_output(
                client,
                agent_url,
                loop_session,
                request_id,
                tick_prompt,
                timeout_sec=timeout_sec,
                session_index=0,
                gateway=gateway,
                post="invoke_tick",
            )
            if err:
                raise RuntimeError(err)
            produced.append(turn)
            if on_turn_complete is not None:
                await on_turn_complete(len(produced) - 1, turn)
        elif step.action == "read_history":
            session_id = loop_session or (produced[-1].session_id if produced else "")
            if not session_id:
                raise RuntimeError("read_history needs a session")
            produced.history_snapshots.append(
                await _read_history(client, base, session_id, timeout)
            )
    return loop_ids


def attach_transcripts(produced: ScenarioRun, workspace: Path | None) -> None:
    """Fill system prompts and child manifests from this run's session transcripts."""
    if workspace is None or not workspace.is_dir():
        return
    from monkeybot.core.persistence.transcript import resolve_session_artifact_dir
    from monkeybot.core.persistence.transcript_reader import resolve_records

    prompts: dict[str, str] = {}
    manifests: list[dict[str, Any]] = []
    seen: list[str] = []
    for turn in produced:
        if turn.session_id and turn.session_id not in seen:
            seen.append(turn.session_id)
    for session_id in seen:
        parent = resolve_session_artifact_dir(workspace, session_id)
        if not parent.is_dir():
            continue
        prompts.update(_prompts_by_request(parent / "transcript.ndjson", resolve_records))
        children = parent / "subagents"
        if not children.is_dir():
            continue
        for path in sorted(children.glob("*/transcript.ndjson")):
            manifest = _last_manifest(path)
            if manifest is not None:
                manifests.append(manifest)
    produced.system_prompts = [prompts.get(turn.request_id, "") for turn in produced]
    produced.manifests = manifests


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _prompts_by_request(
    path: Path, resolve_records: Callable[..., list[dict[str, Any]]]
) -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        records = resolve_records(_records(path))
    except (OSError, ValueError, TypeError):
        return found
    for record in records:
        if record.get("type") != "SystemPromptSnapshot":
            continue
        request_id = record.get("request_id")
        text = record.get("text")
        if isinstance(request_id, str) and request_id and isinstance(text, str) and text.strip():
            found.setdefault(request_id, text)
    return found


def _last_manifest(path: Path) -> dict[str, Any] | None:
    last: dict[str, Any] | None = None
    for record in _records(path):
        if record.get("type") == "SessionManifest":
            last = record
    return last


async def run_scenario_live(
    scenario: Scenario,
    agent_url: str,
    *,
    timeout_sec: float = 180.0,
    on_turn_complete: Callable[[int, TurnResult], Awaitable[None]] | None = None,
    gateway: Gateway | None = None,
) -> ScenarioRun:
    """Send each message group through its own session; run live steps around them.

    Steps without ``when`` run first (create a loop, invoke one tick, read history).
    Steps with ``when`` fire during the session they belong to. Empty message groups
    are skipped, so a scheduler scenario can have steps and no ``messages``.
    """
    timeout = httpx.Timeout(timeout_sec, connect=30.0)
    produced = ScenarioRun()
    async with httpx.AsyncClient(timeout=timeout) as client:
        loop_ids = await _run_setup(
            client,
            agent_url,
            scenario,
            produced,
            gateway,
            timeout_sec,
            on_turn_complete,
        )
        groups = [group for group in scenario.message_groups() if group]
        for index, group in enumerate(groups):
            if index > 0 and scenario.session_pause_sec > 0:
                await asyncio.sleep(scenario.session_pause_sec)
            group_turns = await _run_session(
                client,
                agent_url,
                group,
                timeout_sec=timeout_sec,
                start_index=len(produced),
                session_index=index + 1,
                due=DueSteps(list(scenario.steps)),
                gateway=gateway,
                on_turn_complete=on_turn_complete,
            )
            produced.extend(group_turns)
        for loop_id in loop_ids:
            await _stop_loop(client, agent_url.rstrip("/"), loop_id, timeout)
    return produced
