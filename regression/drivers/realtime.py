"""Drive ``driver: realtime`` scenarios through the in-process realtime app.

The provider is :class:`~monkeybot.core.testing.mocks_realtime_provider.ScriptedRealtimeProvider`.
Nothing here opens a Gemini Live socket or reads GCP credentials. T4-RT-01 stays deferred.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import yaml
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from monkeybot.core.config.runtime_env import reset_runtime_env_state_for_tests
from monkeybot.core.llm.provider import Message
from monkeybot.core.llm.realtime_provider import (
    RealtimeEvent,
    RealtimeTextDelta,
    RealtimeToolCall,
    RealtimeTurnBoundary,
    RealtimeUsage,
)
from monkeybot.core.runtime.history_rewrite import resolve_active_thread_id
from monkeybot.core.testing.mocks_realtime_provider import (
    ScriptedRealtimeProvider,
    ScriptedRealtimeSession,
)
from regression.drivers.support.config import _config_overrides, _merge_overrides
from regression.drivers.support.paths import _BASE_CONFIG
from regression.schema.assertions import RunView, evaluate, event_type
from regression.schema.events import DoneEvent, TextEvent, ToolCallEvent, UsageEvent
from regression.schema.invariants import check_invariants
from regression.schema.scenario import Scenario
from regression.schema.steps import (
    ClientTextStep,
    ConfirmToolStep,
    EventPredicate,
    InterruptStep,
    Step,
    predicate_matches,
)

_FRAME_TIMEOUT_S = 20.0
_ACTIONS = frozenset({"client_text", "interrupt", "confirm_tool"})
_ISOLATION: dict[str, str] = {
    "MODEL_PROVIDER": "fake",
    "MONKEYBOT_OTEL_ENABLED": "false",
    "ATTACHMENTS_ENABLED": "false",
    "MONKEYBOT_SCHEDULER_ENABLED": "false",
    "MONKEYBOT_COMPUTER_TOOLS": "false",
    "MONKEYBOT_MEMORY_HOOK_ENABLED": "false",
    "WEB_SEARCH_BACKEND": "none",
    "SANDBOX_ENABLED": "false",
    "MONKEYBOT_WORKER_POOL": "false",
}
_DROP = (
    "MONKEYBOT_WORKSPACE_ROOT_OVERRIDE",
    "MONKEYBOT_FAKE_PROVIDER_EVENTS",
    "MONKEYBOT_FAKE_PROVIDER_SCRIPT",
    "MONKEYBOT_AGENT_ID",
    "ARTIFACTS_PATH",
    "MEMORY_STORAGE_URI",
    "MEMORY_PATH",
    "MEMPALACE_PALACE_PATH",
    "MEMPALACE_BACKEND",
    "COMMAND_ALLOWLIST_CONFIG",
    "PERMISSION_CONFIG",
    "MONKEYBOT_APPROVALS_CONFIG",
    "MONKEYBOT_GRANTS_CONFIG",
    "MONKEYBOT_REALTIME_SESSION_MAX_CONCURRENT_SESSIONS",
    "MONKEYBOT_REALTIME_SESSION_MAX_DURATION_SEC",
    "MONKEYBOT_REALTIME_SESSION_IDLE_TIMEOUT_SEC",
    "MONKEYBOT_REALTIME_SESSION_MAX_RESPONSE_TURN_SEC",
)
_STOP = frozenset({"interrupt", "close"})


class RealtimeDriverError(RuntimeError):
    """The realtime driver could not run the scenario."""


class PacedRealtimeSession(ScriptedRealtimeSession):
    """Yield one scripted turn after the client sends text or tool results.

    ``ScriptedRealtimeSession.events`` dumps every event immediately and the
    route then tears the socket down. This subclass waits so connect, text,
    and an interrupt land in that order. The queue is created on first use,
    which is the server loop.
    """

    def __init__(self, turns: Sequence[Sequence[RealtimeEvent]], *, pause_after_text: bool) -> None:
        flat = [event for turn in turns for event in turn]
        super().__init__(flat)
        self._turns = [list(turn) for turn in turns]
        self._pause_after_text = pause_after_text
        self._go: asyncio.Queue[str] | None = None
        self._interrupted = False
        self.finished = 0

    def _queue(self) -> asyncio.Queue[str]:
        if self._go is None:
            self._go = asyncio.Queue()
        return self._go

    async def send_text(self, text: str) -> None:
        await super().send_text(text)
        await self._queue().put("text")

    async def send_tool_results(
        self,
        results: Sequence[tuple[str, str, dict[str, object], bool]],
    ) -> None:
        await super().send_tool_results(results)
        await self._queue().put("tools")

    async def interrupt(self) -> None:
        self._interrupted = True
        await super().interrupt()
        await self._queue().put("interrupt")

    async def close(self, *, reason: str = "session_end") -> None:
        await super().close(reason=reason)
        await self._queue().put("close")

    def events(self) -> AsyncIterator[RealtimeEvent]:
        async def _gen() -> AsyncIterator[RealtimeEvent]:
            while True:
                signal = await self._queue().get()
                if signal in _STOP or self._interrupted:
                    return
                if self.finished >= len(self._turns):
                    continue
                for event in self._turns[self.finished]:
                    # Count the turn before the boundary yield. The route commits
                    # history and sends usage while this yield is suspended.
                    if isinstance(event, RealtimeTurnBoundary):
                        self.finished += 1
                    yield event
                    if not self._pause_after_text or not isinstance(event, RealtimeTextDelta):
                        continue
                    nxt = await self._queue().get()
                    if nxt in _STOP or self._interrupted:
                        return

        return _gen()


@dataclass
class _Due:
    """Fire each step on its nth matching frame, then drop it."""

    steps: list[Step]
    seen: dict[int, int]

    def __init__(self, steps: list[Step]) -> None:
        self.steps = steps
        self.seen = {}

    def take(self, event: Mapping[str, Any]) -> Step | None:
        for step in self.steps:
            if not _step_matches(step.when, event):
                continue
            key = id(step)
            count = self.seen.get(key, 0) + 1
            self.seen[key] = count
            if count < step.when.nth:
                return None
            self.steps.remove(step)
            return step
        return None


async def run_scenario(scenario: Scenario) -> None:
    """Run one realtime scenario. A failed invariant or assertion raises."""
    _reject(scenario)
    with tempfile.TemporaryDirectory(prefix="mb-realtime-") as raw:
        root = Path(raw)
        _install_layout(root, scenario)
        previous = _push_env(_env(root, scenario))
        reset_runtime_env_state_for_tests()
        try:
            _run_app(scenario, root)
        finally:
            _pop_env(previous)
            reset_runtime_env_state_for_tests()


def _reject(scenario: Scenario) -> None:
    if scenario.mode != "scripted":
        raise RealtimeDriverError(
            f"realtime driver runs scripted scenarios, not mode {scenario.mode!r}"
        )
    if scenario.driver != "realtime":
        raise RealtimeDriverError(f"realtime driver cannot run driver {scenario.driver!r}")
    if scenario.sessions is not None:
        raise RealtimeDriverError("realtime driver runs one websocket session")
    for step in scenario.steps:
        if step.action not in _ACTIONS:
            raise RealtimeDriverError(
                f"realtime driver does not run {step.action}; "
                "steps are client_text, interrupt, and confirm_tool"
            )


def _run_app(scenario: Scenario, root: Path) -> None:
    from monkeybot.gateway.realtime import routes as realtime_routes
    from monkeybot.gateway.realtime.app import create_realtime_app

    app = create_realtime_app()
    original = realtime_routes.run_guardrails

    async def _fast(state: Any, config: Any, *, poll_interval_sec: float = 5.0) -> None:
        del poll_interval_sec
        await original(state, config, poll_interval_sec=0.05)

    realtime_routes.run_guardrails = _fast
    try:
        with TestClient(app) as client:
            _swap_provider(app, _session(scenario))
            session_id = f"rt-{uuid.uuid4().hex}"
            events = _drive(client, app, scenario, session_id)
            history = _history(client, app, session_id)
            view = RunView(
                events=events,
                workspace=root / "workspace",
                history=history,
                scripted=True,
                script_remaining=_remaining(app),
                script_partial=bool(scenario.script_partial),
                driver="realtime",
            )
            _check(scenario, view)
    finally:
        realtime_routes.run_guardrails = original


def _session(scenario: Scenario) -> PacedRealtimeSession:
    pause = any(isinstance(step, InterruptStep) for step in scenario.steps)
    return PacedRealtimeSession(_script_turns(scenario), pause_after_text=pause)


def _swap_provider(app: Any, session: PacedRealtimeSession) -> None:
    deps = app.state.realtime_deps
    provider = ScriptedRealtimeProvider(name="fake-realtime", sessions=[session])
    object.__setattr__(deps, "realtime_provider", provider)
    app.state.realtime_script = session


def _drive(
    client: TestClient, app: Any, scenario: Scenario, session_id: str
) -> list[dict[str, Any]]:
    mode = _mode(scenario, app)
    events: list[dict[str, Any]] = []
    due = _Due(list(scenario.steps))
    if mode == "slot":
        _drive_slot(client, session_id, events, due)
        return events
    url = f"/sessions/{session_id}/realtime"
    with client.websocket_connect(url) as ws:
        _emit(ws, {"kind": "connect", "session_id": session_id}, events, due)
        _read_until(ws, events, due, stop=_has("connected"))
        if mode == "guardrail":
            _read_until(ws, events, due, stop=_has("session_ended"))
        elif mode == "history":
            _delete_history(client, session_id, events)
        else:
            if len(scenario.messages) != 1 or not scenario.script:
                raise RealtimeDriverError("realtime turn scenario needs one message and a script")
            _emit(ws, {"kind": "text", "text": scenario.messages[0]}, events, due)
            _read_until(ws, events, due, stop=_turn_done(_boundaries(scenario)))
    return events


def _drive_slot(
    client: TestClient,
    session_id: str,
    events: list[dict[str, Any]],
    due: _Due,
) -> None:
    holder = f"/sessions/{session_id}/realtime"
    other = f"/sessions/{session_id}-b/realtime"
    with client.websocket_connect(holder) as ws:
        _emit(ws, {"kind": "connect", "session_id": session_id}, events, due)
        _read_until(ws, events, due, stop=_has("connected"))
        try:
            with client.websocket_connect(other) as rejected:
                events.append(
                    {
                        "type": "upgrade_rejected",
                        "kind": "upgrade_rejected",
                        "status": 101,
                        "accepted": True,
                    }
                )
                rejected.close()
        except WebSocketDenialResponse as denial:
            events.append(
                {
                    "type": "upgrade_rejected",
                    "kind": "upgrade_rejected",
                    "status": denial.status_code,
                    "accepted": False,
                }
            )


def _delete_history(client: TestClient, session_id: str, events: list[dict[str, Any]]) -> None:
    path = f"/api/chat-history/{session_id}"
    response = client.delete(path)
    events.append(
        {
            "type": "chat_history_delete",
            "kind": "chat_history_delete",
            "status": response.status_code,
            "path": path,
        }
    )


def _mode(scenario: Scenario, app: Any) -> str:
    session = app.state.realtime_config.session
    quiet = not scenario.messages and not scenario.script
    if quiet and session.max_concurrent_sessions < 100:
        return "slot"
    if quiet and (session.max_duration_sec <= 0 or session.idle_timeout_sec <= 0):
        return "guardrail"
    if scenario.case == "T2-RT-07" and quiet:
        return "history"
    return "turn"


def _emit(ws: Any, payload: dict[str, Any], events: list[dict[str, Any]], due: _Due) -> None:
    ws.send_json(payload)
    events.append(_record(payload))
    step = due.take(events[-1])
    if step is not None:
        _apply(ws, step, events, due)


def _apply(ws: Any, step: Step, events: list[dict[str, Any]], due: _Due) -> None:
    if isinstance(step, InterruptStep):
        _emit(ws, {"kind": "interrupt"}, events, due)
        return
    if isinstance(step, ClientTextStep):
        _emit(ws, {"kind": "text", "text": step.text}, events, due)
        return
    if isinstance(step, ConfirmToolStep):
        call_id = step.call_id or step.when.call_id or ""
        _emit(
            ws,
            {
                "kind": "tool_confirmation_response",
                "tool_call_id": call_id,
                "approved": step.decision != "deny",
                "reason": step.reason or "",
            },
            events,
            due,
        )
        return
    raise RealtimeDriverError(f"realtime driver does not run {step.action}")


def _read_until(
    ws: Any,
    events: list[dict[str, Any]],
    due: _Due,
    *,
    stop: Any,
) -> None:
    while not stop(events):
        frame = _next_frame(ws)
        if frame is None:
            return
        events.append(frame)
        step = due.take(frame)
        if step is not None:
            _apply(ws, step, events, due)


def _next_frame(ws: Any) -> dict[str, Any] | None:
    async def _recv() -> dict[str, Any]:
        with anyio.fail_after(_FRAME_TIMEOUT_S):
            message: dict[str, Any] = await ws._send_rx.receive()
            return message

    try:
        message = ws.portal.call(_recv)
    except TimeoutError as exc:
        raise RealtimeDriverError("timed out waiting for a realtime frame") from exc
    try:
        ws._raise_on_close(message)
    except WebSocketDisconnect:
        return None
    if message.get("type") != "websocket.send":
        kind = message.get("type")
        raise RealtimeDriverError(f"unexpected websocket message {kind!r}")
    text = message.get("text")
    if not isinstance(text, str):
        return {"type": "audio", "kind": "audio"}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise RealtimeDriverError("realtime frame was not an object")
    return _record(parsed)


def _record(payload: Mapping[str, Any]) -> dict[str, Any]:
    kind = payload.get("kind", payload.get("type", ""))
    recorded = dict(payload)
    recorded["type"] = kind if isinstance(kind, str) else ""
    recorded["kind"] = recorded["type"]
    return recorded


def _has(kind: str) -> Any:
    def _stop(events: list[dict[str, Any]]) -> bool:
        return any(event.get("type") == kind for event in events)

    return _stop


def _turn_done(boundaries: int) -> Any:
    def _stop(events: list[dict[str, Any]]) -> bool:
        kinds = [event.get("type") for event in events]
        if "interrupted" in kinds or "session_ended" in kinds:
            return True
        return boundaries > 0 and kinds.count("usage") >= boundaries

    return _stop


def _step_matches(pred: EventPredicate, event: Mapping[str, Any]) -> bool:
    if predicate_matches(pred, event):
        return True
    if event_type(event) != pred.event:
        return False
    if pred.name is not None:
        name = event.get("name") or event.get("tool_name")
        if name != pred.name:
            return False
    if pred.call_id is not None:
        call_id = event.get("call_id") or event.get("tool_call_id")
        if call_id != pred.call_id:
            return False
    return True


def _script_turns(scenario: Scenario) -> list[list[RealtimeEvent]]:
    turns: list[list[RealtimeEvent]] = []
    for turn in scenario.script or []:
        mapped: list[RealtimeEvent] = []
        for item in turn:
            mapped.append(_provider_event(item))
        turns.append(mapped)
    return turns


def _provider_event(item: Any) -> RealtimeEvent:
    if isinstance(item, TextEvent):
        return RealtimeTextDelta(text=item.text)
    if isinstance(item, ToolCallEvent):
        return RealtimeToolCall(
            call_id=item.call_id,
            name=item.name,
            args=dict(item.args),
            parse_error=item.parse_error,
        )
    if isinstance(item, DoneEvent):
        if item.truncated:
            raise RealtimeDriverError(
                "realtime script cannot request Done.truncated; the product rejects "
                "batches with truncated=False"
            )
        return RealtimeTurnBoundary(role="assistant")
    if isinstance(item, UsageEvent):
        return RealtimeUsage(input_tokens=item.input_tokens, output_tokens=item.output_tokens)
    kind = getattr(item, "type", type(item).__name__)
    raise RealtimeDriverError(f"realtime script does not play {kind}")


def _boundaries(scenario: Scenario) -> int:
    count = 0
    for turn in scenario.script or []:
        if any(isinstance(item, DoneEvent) for item in turn):
            count += 1
    return count


def _remaining(app: Any) -> int:
    session = getattr(app.state, "realtime_script", None)
    if not isinstance(session, PacedRealtimeSession):
        return 0
    return max(0, len(session._turns) - session.finished)


def _history(client: TestClient, app: Any, session_id: str) -> list[Message]:
    async def _load() -> list[Message]:
        storage = app.state.storage
        if storage is None:
            raise RealtimeDriverError("realtime storage was not initialized")
        thread_id = await resolve_active_thread_id(storage, session_id)
        rows = await storage.history().load(thread_id)
        return [row for row in rows if isinstance(row, Message)]

    loaded = client.portal.call(_load)
    if not isinstance(loaded, list):
        raise RealtimeDriverError("history load did not return a list")
    return loaded


def _check(scenario: Scenario, view: RunView) -> None:
    problems = check_invariants(view, scenario.invariants_skip)
    problems.extend(evaluate(scenario.assertions, view))
    if problems:
        seen = ", ".join(event_type(event) or "?" for event in view.events)
        detail = "\n".join(problems)
        raise AssertionError(f"{scenario.case}\n{detail}\nsaw: {seen}")


def _install_layout(root: Path, scenario: Scenario) -> None:
    if not _BASE_CONFIG.is_file():
        raise RealtimeDriverError(f"base agent config is missing: {_BASE_CONFIG}")
    loaded = yaml.safe_load(_BASE_CONFIG.read_text(encoding="utf-8"))
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise RealtimeDriverError("agent.base.yaml root must be a mapping")
    overrides, _env_overrides = _config_overrides(scenario)
    merged = _merge_overrides(loaded, overrides)
    model = dict(merged.get("model") or {})
    model["provider"] = "fake"
    merged["model"] = model
    config_dir = root / "monkeybot_config"
    config_dir.mkdir(parents=True)
    workspace = root / "workspace"
    workspace.mkdir()
    (root / "skills").mkdir()
    (root / "data").mkdir()
    (workspace / "note.txt").write_text("hello from the note\n", encoding="utf-8")
    (config_dir / "monkeybot.yaml").write_text(
        yaml.safe_dump(merged, sort_keys=False),
        encoding="utf-8",
    )
    (config_dir / "AGENT.md").write_text("# Agent\n", encoding="utf-8")
    (config_dir / "mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    _install_permissions(config_dir, scenario)


def _install_permissions(config_dir: Path, scenario: Scenario) -> None:
    policy = scenario.policy
    if policy is None or not policy.permissions:
        return
    source = _BASE_CONFIG.parent / f"permissions.{policy.permissions}.yaml"
    if not source.is_file():
        raise RealtimeDriverError(
            f"permissions fixture {policy.permissions!r} is missing ({source})"
        )
    target = config_dir / "permissions.yaml"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _env(root: Path, scenario: Scenario) -> dict[str, str]:
    _overrides, scenario_env = _config_overrides(scenario)
    updates = dict(_ISOLATION)
    updates.update({key: value for key, value in scenario_env.items() if isinstance(value, str)})
    config_dir = root / "monkeybot_config"
    updates["MONKEYBOT_CONFIG"] = str(config_dir / "monkeybot.yaml")
    updates["MONKEYBOT_AGENT_ROOT"] = str(root)
    updates["DB_URL"] = "sqlite:///data/monkeybot.db"
    updates["AGENT_MD"] = str(config_dir / "AGENT.md")
    updates["MCP_CONFIG"] = str(config_dir / "mcp.json")
    updates["SKILLS_PATH"] = str(root / "skills")
    updates["MONKEYBOT_WORKSPACE_ROOT"] = str(root / "workspace")
    return updates


def _push_env(updates: Mapping[str, str]) -> dict[str, str | None]:
    previous: dict[str, str | None] = {}
    keys = set(updates) | set(_DROP)
    for key in keys:
        previous[key] = os.environ.get(key)
    for key in _DROP:
        os.environ.pop(key, None)
    for key, value in updates.items():
        os.environ[key] = value
    return previous


def _pop_env(previous: Mapping[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


__all__ = ["PacedRealtimeSession", "RealtimeDriverError", "run_scenario"]
