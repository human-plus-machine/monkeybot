"""Drive ``driver: gateway`` scenarios through the in-process SSE app.

One session, ``POST /reply`` per message, then the streaming reader. SSE steps
``disconnect_sse`` and ``reconnect_sse`` run here. The loop driver rejects them.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

from monkeybot.core.llm.provider import Message
from monkeybot.core.runtime.history_rewrite import resolve_active_thread_id
from monkeybot.core.testing.mocks_provider import ScriptedProvider
from monkeybot.gateway.sse.app import gateway_runtime
from regression.drivers.support.config import _config_overrides, _merge_overrides
from regression.drivers.support.paths import _BASE_CONFIG
from regression.schema.assertions import RunView, evaluate, event_type
from regression.schema.invariants import check_invariants
from regression.schema.scenario import Scenario
from regression.schema.steps import (
    DisconnectSseStep,
    ReconnectSseStep,
    Step,
    loop_driver_rejection,
    predicate_matches,
)
from testkit.gateway import GatewaySession, open_gateway
from testkit.sse import SseFrame, SseParser, SseStreamError, StreamingAsgiResponse

_SSE_ACTIONS = frozenset({"disconnect_sse", "reconnect_sse"})
_REALTIME_ACTIONS = frozenset({"client_text", "interrupt"})
_FRAME_TIMEOUT_S = 20.0
_SKIP_EVENT_TYPES = frozenset({"ActiveRequests", "ping", "comment"})


class GatewayDriverError(RuntimeError):
    """The gateway driver cannot run this scenario, or the stream stalled."""


@dataclass
class GatewayRun:
    """Events read from the SSE stream, and the view invariants saw."""

    events: list[object]
    view: RunView


@dataclass
class _DueSteps:
    """Fire each SSE step once, on the nth matching event."""

    steps: list[Step]
    _pending: list[Step] = field(init=False)
    _counts: dict[tuple[str, str, str], int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._pending = list(self.steps)

    def take(self, event: object) -> list[Step]:
        if not self._pending:
            return []
        fired: list[Step] = []
        still: list[Step] = []
        seen: set[tuple[str, str, str]] = set()
        for step in self._pending:
            if not predicate_matches(step.when, event):
                still.append(step)
                continue
            key = (step.when.event, step.when.name or "", step.when.call_id or "")
            if key not in seen:
                self._counts[key] = self._counts.get(key, 0) + 1
                seen.add(key)
            if self._counts[key] == step.when.nth:
                fired.append(step)
            else:
                still.append(step)
        self._pending = still
        return fired


class _Connection:
    """One ``GET /events`` body, parsed as frames arrive."""

    def __init__(self, stream: StreamingAsgiResponse) -> None:
        self._stream = stream
        self._parser = SseParser()
        self._pending: list[SseFrame] = []
        self._chunks = stream.__aiter__()
        self._exhausted = False

    async def next_frame(self) -> SseFrame | None:
        while not self._pending and not self._exhausted:
            try:
                chunk = await self._chunks.__anext__()
            except StopAsyncIteration:
                self._exhausted = True
                self._pending.extend(self._parser.finish())
                break
            self._pending.extend(self._parser.feed(chunk))
        if not self._pending:
            return None
        return self._pending.pop(0)

    async def aclose(self) -> None:
        await self._stream.aclose()


class _Link:
    """The live SSE connection. Steps replace it on reconnect."""

    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.replay_for: str | None = None


async def run_scenario(scenario: Scenario) -> GatewayRun:
    """Run one gateway scenario. A failed invariant or assertion raises."""
    _reject(scenario)
    config, env = _scenario_document(scenario)
    async with open_gateway(script=_script_text(scenario), config=config, env=env) as gateway:
        return await _drive(scenario, gateway)


def _reject(scenario: Scenario) -> None:
    if scenario.mode != "scripted":
        raise GatewayDriverError(
            f"gateway driver runs scripted scenarios, not mode {scenario.mode!r}"
        )
    if scenario.driver != "gateway":
        raise GatewayDriverError(f"gateway driver cannot run driver {scenario.driver!r}")
    if scenario.sessions is not None:
        raise GatewayDriverError("gateway driver runs the messages list as one session")
    if not scenario.messages:
        raise GatewayDriverError("scripted gateway scenario needs at least one message")
    if not scenario.script:
        raise GatewayDriverError("scripted gateway scenario is missing script")
    for step in scenario.steps:
        if step.action in _SSE_ACTIONS:
            continue
        if step.action in _REALTIME_ACTIONS:
            reason = loop_driver_rejection(step.action)
            raise GatewayDriverError(
                reason
                or (
                    f"gateway driver does not run {step.action}; "
                    "client_text and interrupt belong to the realtime driver (P2-05)"
                )
            )
        raise GatewayDriverError(
            f"gateway driver does not run {step.action}; "
            "SSE steps are disconnect_sse and reconnect_sse"
        )


def _scenario_document(scenario: Scenario) -> tuple[dict[str, Any], dict[str, str]]:
    if not _BASE_CONFIG.is_file():
        raise GatewayDriverError(f"base agent config is missing: {_BASE_CONFIG}")
    loaded = yaml.safe_load(_BASE_CONFIG.read_text(encoding="utf-8"))
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise GatewayDriverError("agent.base.yaml root must be a mapping")
    overrides, env = _config_overrides(scenario)
    merged = _merge_overrides(loaded, overrides)
    return merged, env


def _script_text(scenario: Scenario) -> str:
    turns: list[list[dict[str, Any]]] = []
    for turn in scenario.script or []:
        turns.append([event.model_dump(exclude_none=True) for event in turn])
    return yaml.safe_dump(turns, sort_keys=False, allow_unicode=True)


async def _drive(scenario: Scenario, gateway: GatewaySession) -> GatewayRun:
    session_id = f"gw-{uuid.uuid4().hex}"
    await gateway.create_session(session_id)
    events: list[object] = []
    sse_frames: list[object] = []
    sse_replay: dict[str, list[str]] = {}
    seen_ids: set[str] = set()
    wire: list[str] = []
    serialized: list[object] = []
    due = _DueSteps(list(scenario.steps))
    link = _Link(await _open(gateway, session_id, None))
    try:
        await _read_turns(
            scenario,
            gateway,
            session_id=session_id,
            link=link,
            due=due,
            events=events,
            sse_frames=sse_frames,
            sse_replay=sse_replay,
            seen_ids=seen_ids,
            wire=wire,
            serialized=serialized,
        )
    finally:
        if link.connection is not None:
            await link.connection.aclose()
            link.connection = None
    history = await _history(gateway, session_id)
    script_remaining, provider_calls = _script_usage(scenario)
    probe = await gateway.post_reply(session_id, "lock-check", "lock check")
    _cancel_probe(gateway, session_id)
    view = RunView(
        events=events,
        workspace=gateway.agent_root / "workspace",
        history=history,
        sse_frames=sse_frames,
        sse_replay=sse_replay,
        scripted=True,
        script_remaining=script_remaining,
        script_partial=bool(scenario.script_partial),
        driver="gateway",
        provider_calls=provider_calls,
        second_reply_status=probe.status_code,
        sse_wire="".join(wire),
        serialized_events=serialized,
    )
    _check(scenario, view)
    return GatewayRun(events=events, view=view)


async def _read_turns(
    scenario: Scenario,
    gateway: GatewaySession,
    *,
    session_id: str,
    link: _Link,
    due: _DueSteps,
    events: list[object],
    sse_frames: list[object],
    sse_replay: dict[str, list[str]],
    seen_ids: set[str],
    wire: list[str],
    serialized: list[object],
) -> None:
    need_open = True
    awaiting_subscribe = True
    index = 0
    request_id = "r1"
    while index < len(scenario.messages):
        connection = link.connection
        if connection is None:
            raise GatewayDriverError("SSE disconnected before TurnComplete")
        if need_open:
            need_open = False
            if await _apply(gateway, session_id, link, due, {"type": "sse_open"}, sse_replay):
                need_open = True
                continue
        frame = await _next_frame(connection, events)
        payload = _record(
            frame,
            link=link,
            events=events,
            sse_frames=sse_frames,
            sse_replay=sse_replay,
            seen_ids=seen_ids,
            wire=wire,
            serialized=serialized,
        )
        if await _apply(gateway, session_id, link, due, payload, sse_replay):
            need_open = True
            continue
        if awaiting_subscribe:
            if payload.get("type") == "ActiveRequests":
                _require_ok(
                    await gateway.post_reply(session_id, request_id, scenario.messages[index])
                )
                awaiting_subscribe = False
            continue
        if _turn_complete(payload, request_id):
            await _wait_turn(gateway, session_id)
            index += 1
            if index >= len(scenario.messages):
                return
            request_id = f"r{index + 1}"
            _require_ok(await gateway.post_reply(session_id, request_id, scenario.messages[index]))


async def _open(
    gateway: GatewaySession,
    session_id: str,
    last_event_id: str | None,
) -> _Connection:
    stream = gateway.stream_events(session_id, last_event_id=last_event_id)
    try:
        await stream.__aenter__()
    except SseStreamError as exc:
        raise GatewayDriverError(str(exc)) from exc
    return _Connection(stream)


async def _apply(
    gateway: GatewaySession,
    session_id: str,
    link: _Link,
    due: _DueSteps,
    event: object,
    sse_replay: dict[str, list[str]],
) -> bool:
    """Run SSE steps for ``event``. True when the live connection was replaced."""
    replaced = False
    for step in due.take(event):
        if isinstance(step, DisconnectSseStep):
            await _close(link)
            replaced = True
            continue
        if isinstance(step, ReconnectSseStep):
            await _close(link)
            link.connection = await _open(gateway, session_id, step.last_event_id)
            link.replay_for = step.last_event_id
            sse_replay.setdefault(step.last_event_id, [])
            replaced = True
    return replaced


async def _close(link: _Link) -> None:
    connection = link.connection
    link.connection = None
    if connection is not None:
        await connection.aclose()


async def _next_frame(connection: _Connection, events: list[object]) -> SseFrame:
    try:
        frame = await asyncio.wait_for(connection.next_frame(), timeout=_FRAME_TIMEOUT_S)
    except TimeoutError:
        seen = [event_type(event) or "?" for event in events]
        raise GatewayDriverError(
            f"timed out waiting for an SSE frame; events so far: {seen}"
        ) from None
    if frame is None:
        seen = [event_type(event) or "?" for event in events]
        raise GatewayDriverError(f"SSE stream ended before TurnComplete; events so far: {seen}")
    return frame


def _record(
    frame: SseFrame,
    *,
    link: _Link,
    events: list[object],
    sse_frames: list[object],
    sse_replay: dict[str, list[str]],
    seen_ids: set[str],
    wire: list[str],
    serialized: list[object],
) -> dict[str, Any]:
    wire.append(_frame_wire(frame))
    if frame.data is not None:
        try:
            body = json.loads(frame.data)
        except json.JSONDecodeError:
            body = None
        if isinstance(body, dict):
            serialized.append(body)
    payload = _payload(frame)
    sse_frames.append(payload)
    kind = payload.get("type")
    if link.replay_for is not None:
        if kind == "ActiveRequests":
            link.replay_for = None
        elif frame.id:
            sse_replay.setdefault(link.replay_for, []).append(frame.id)
    if kind in _SKIP_EVENT_TYPES:
        return payload
    if frame.id and frame.id in seen_ids:
        return payload
    if frame.id:
        seen_ids.add(frame.id)
    events.append(payload)
    return payload


def _frame_wire(frame: SseFrame) -> str:
    """Rebuild the SSE bytes the gateway wrote for one frame."""
    lines: list[str] = []
    if frame.id is not None:
        lines.append(f"id: {frame.id}")
    if frame.event is not None:
        lines.append(f"event: {frame.event}")
    if frame.data is not None:
        lines.extend(f"data: {part}" for part in frame.data.split("\n"))
    if frame.comment is not None:
        lines.extend(f": {part}" for part in frame.comment.split("\n"))
    return "\n".join(lines) + "\n\n"


def _payload(frame: SseFrame) -> dict[str, Any]:
    if frame.data is None:
        comment = frame.comment or ""
        kind = "ping" if comment.startswith("ping") else "comment"
        return {"type": kind, "comment": comment}
    try:
        parsed = json.loads(frame.data)
    except json.JSONDecodeError:
        parsed = {"type": "data", "data": frame.data}
    if not isinstance(parsed, dict):
        parsed = {"type": "data", "data": parsed}
    if frame.id:
        parsed = {**parsed, "id": frame.id}
    return parsed


def _turn_complete(payload: Mapping[str, Any], request_id: str) -> bool:
    if payload.get("type") != "TurnComplete":
        return False
    rid = payload.get("request_id")
    return not isinstance(rid, str) or rid == "" or rid == request_id


def _require_ok(response: Any) -> None:
    status = getattr(response, "status_code", None)
    if status == 200:
        return
    body = ""
    text = getattr(response, "text", "")
    if isinstance(text, str):
        body = text.replace("\n", " ")[:300]
    raise GatewayDriverError(f"POST /reply returned HTTP {status}: {body}")


def _cancel_probe(gateway: GatewaySession, session_id: str) -> None:
    """Drop the lock-check turn so shutdown does not wait it out.

    The HTTP status is already recorded. The probe exists to show the turn
    lock was released; its script is not part of the scenario.
    """
    registry = getattr(gateway.app.state, "registry", None)
    bus = None if registry is None else registry.get(session_id)
    if bus is not None:
        bus.request_cancel("lock-check")


async def _wait_turn(gateway: GatewaySession, session_id: str) -> None:
    registry = getattr(gateway.app.state, "registry", None)
    bus = None if registry is None else registry.get(session_id)
    task = None if bus is None else bus.active_turn_task
    if task is None:
        return
    await task


async def _history(gateway: GatewaySession, session_id: str) -> list[Message]:
    backend = getattr(gateway.app.state, "storage", None)
    if backend is None:
        raise GatewayDriverError("gateway storage was not initialized")
    thread_id = await resolve_active_thread_id(backend, session_id)
    rows = await backend.history().load(thread_id)
    return [row for row in rows if isinstance(row, Message)]


def _script_usage(scenario: Scenario) -> tuple[int, int]:
    provider = gateway_runtime.provider
    script_len = len(scenario.script or [])
    if not isinstance(provider, ScriptedProvider):
        return script_len, 0
    return max(0, script_len - provider.stream_calls), provider.stream_calls


def _check(scenario: Scenario, view: RunView) -> None:
    problems = check_invariants(view, scenario.invariants_skip)
    problems.extend(evaluate(scenario.assertions, view))
    if problems:
        detail = "\n".join(problems)
        raise AssertionError(f"{scenario.case}\n{detail}")


__all__ = ["GatewayDriverError", "GatewayRun", "run_scenario"]
