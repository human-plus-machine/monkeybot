"""P2-04 gateway cases the scripted YAML driver cannot assert.

The driver posts one ``/reply`` per message and can reconnect SSE. These tests
drive the other routes on the in-process app. Assertions match the catalog
rows; a product disagreement is left failing.
"""

from __future__ import annotations

import os
import tempfile
import time
from typing import Any

import pytest
import yaml

from monkeybot.core.goals.service import DurableGoalService
from monkeybot.core.memory.subsystem import MemoryConfigurationError
from monkeybot.core.runtime.events import (
    ActionRequiredEvent,
    FrontendToolRequestEvent,
    event_to_json,
)
from monkeybot.core.types.content_blocks import ToolRequest, ToolResponse
from monkeybot.gateway.realtime.manager import RealtimeSessionManager
from monkeybot.gateway.sse.app import gateway_runtime
from regression.t2.gateway.harness import (
    FrameReader,
    error_code,
    history,
    listening,
    message_text,
    payload,
    remote_client,
    user_row_id,
    wait_turn,
)
from testkit.gateway import GatewaySession, open_gateway

_HELLO = """\
- - type: text
    text: hello
  - type: done
"""
_ASK = """\
- - type: tool_call
    call_id: ask1
    name: ask_user
    args:
      question: Need a name
  - type: done
- - type: text
    text: resumed
  - type: done
- - type: text
    text: next-reply
  - type: done
"""
_LOOP_CONFIRM = """\
- - type: tool_call
    call_id: loop1
    name: start_loop
    args:
      prompt: ping
      interval: 1h
      max_ticks: 1
  - type: done
"""
_TOOL_THEN_TEXT = """\
- - type: tool_call
    call_id: c1
    name: todo_list
    args:
      action: add
      text: Write notes
  - type: done
- - type: text
    text: noted
  - type: done
- - type: text
    text: replayed
  - type: done
"""
_TOOL_PAIR_REPLAY = """\
- - type: tool_call
    call_id: c1
    name: todo_list
    args:
      action: add
      text: Write notes
  - type: done
- - type: text
    text: noted
  - type: done
- - type: tool_call
    call_id: c2
    name: todo_list
    args:
      action: add
      text: Replay notes
  - type: done
- - type: text
    text: replayed
  - type: done
"""
_USAGE = """\
- - type: text
    text: counted
  - type: usage
    input_tokens: 3
    output_tokens: 5
  - type: done
"""
_TITLE = """\
- - type: text
    text: hello
  - type: done
- - type: text
    text: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
  - type: done
"""
_TITLE_FAIL = """\
- - type: text
    text: hello
  - type: done
"""
_BRANCHES = """\
- - type: text
    text: one
  - type: done
- - type: text
    text: two
  - type: done
- - type: text
    text: again
  - type: done
- - type: text
    text: regen
  - type: done
- - type: text
    text: edited
  - type: done
"""
_QUEUE = """\
- - type: text
    text: started-now
  - type: done
- - type: tool_call
    call_id: ask1
    name: ask_user
    args:
      question: Need a name
  - type: done
- - type: text
    text: after-promote
  - type: done
"""


def _rewrite_config(gateway: GatewaySession, mutate: Any) -> None:
    """Change the on-disk config the reload route rebuilds. Env patches do not."""
    path = gateway.agent_root / "monkeybot_config" / "monkeybot.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AssertionError("gateway config was not a mapping")
    mutate(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _set_model_name(name: str):
    def mutate(document: dict[str, Any]) -> None:
        model = document.get("model")
        if not isinstance(model, dict):
            raise AssertionError("model section was not a mapping")
        model["name"] = name

    return mutate


def _set_db_url(url: str):
    def mutate(document: dict[str, Any]) -> None:
        paths = document.get("paths")
        if not isinstance(paths, dict):
            paths = {}
            document["paths"] = paths
        paths["db_url"] = url

    return mutate


def _chain_has(exc: BaseException, kind: type[BaseException]) -> bool:
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        if isinstance(current, kind):
            return True
        grouped = getattr(current, "exceptions", None)
        if isinstance(grouped, tuple):
            pending.extend(item for item in grouped if isinstance(item, BaseException))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        context = current.__context__
        if context is not None and context is not current.__cause__:
            pending.append(context)
    return False


def _frame(frames: list[dict[str, Any]], kind: str, **fields: object) -> dict[str, Any]:
    for frame in frames:
        if frame.get("type") != kind:
            continue
        if all(frame.get(key) == value for key, value in fields.items()):
            return frame
    seen = [frame.get("type") for frame in frames]
    raise AssertionError(f"no {kind} {fields!r} in {seen}")


def _paired(rows: list[Any]) -> None:
    requests: set[str] = set()
    responses: set[str] = set()
    for row in rows:
        for block in row.content:
            if isinstance(block, ToolRequest) and block.id:
                requests.add(block.id)
            elif isinstance(block, ToolResponse) and block.id:
                responses.add(block.id)
    assert requests, "expected a tool pair on the active branch"
    assert requests == responses


class _RecordingProvider:
    """Record the ``model`` argument of each ``stream`` call."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.models: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model", "")
        self.models.append(model if isinstance(model, str) else "")
        return self._inner.stream(*args, **kwargs)


async def _reply(gateway: GatewaySession, session_id: str, request_id: str, message: str) -> None:
    response = await gateway.post_reply(session_id, request_id, message)
    assert response.status_code == 200, response.text[:300]


async def _turn(
    gateway: GatewaySession,
    reader: FrameReader,
    session_id: str,
    request_id: str,
    message: str,
) -> list[dict[str, Any]]:
    await _reply(gateway, session_id, request_id, message)
    frames = await reader.until("TurnComplete")
    await wait_turn(gateway, session_id)
    return frames


async def _answer(gateway: GatewaySession, session_id: str, call_id: str, answer: str) -> None:
    response = await gateway.client.post(
        f"/sessions/{session_id}/ask-user/{call_id}",
        json={"answer": answer},
    )
    assert response.status_code == 202, response.text[:300]


@pytest.mark.case("T2-GW-03")
async def test_setup_failure_releases_lock_and_keeps_prompt() -> None:
    """Empty AGENT.md is a setup failure after the thread id exists."""
    async with open_gateway(script=_HELLO) as gateway:
        await gateway.create_session("s-03")
        agent_md = gateway.agent_root / "monkeybot_config" / "AGENT.md"
        agent_md.write_text(" \n", encoding="utf-8")
        async with listening(gateway, "s-03") as reader:
            await reader.until("ActiveRequests")
            await _reply(gateway, "s-03", "r1", "persist me")
            frames = await reader.until("TurnComplete")
            kinds = [frame.get("type") for frame in frames]
            assert "Error" in kinds
            assert "AssistantDelta" not in kinds
            assert kinds[-1] == "TurnComplete"
            error = _frame(frames, "Error")
            assert "AGENT.md" in str(error.get("error", ""))
            usage = frames[-1]["usage"]
            assert usage["input_tokens"] == 0
            assert usage["output_tokens"] == 0
            rows = await history(gateway, "s-03")
            assert any("persist me" in message_text(row) for row in rows if row.role == "user")
            agent_md.write_text("# Agent\n", encoding="utf-8")
            second = await gateway.post_reply("s-03", "r2", "again")
            assert second.status_code != 409
            assert second.status_code == 200
            await reader.until("TurnComplete")
            await wait_turn(gateway, "s-03")


@pytest.mark.case("T2-GW-04")
async def test_second_reply_during_turn_is_409() -> None:
    async with open_gateway(script=_ASK) as gateway:
        await gateway.create_session("s-04")
        async with listening(gateway, "s-04") as reader:
            await reader.until("ActiveRequests")
            await _reply(gateway, "s-04", "r1", "ask")
            asked = await reader.until("AskUserRequest")
            busy = await gateway.post_reply("s-04", "r2", "again")
            assert busy.status_code == 409
            assert error_code(busy) == "SESSION_BUSY"
            call_id = str(_frame(asked, "AskUserRequest").get("tool_call_id"))
            await _answer(gateway, "s-04", call_id, "yes")
            await reader.until("TurnComplete")
            await wait_turn(gateway, "s-04")


@pytest.mark.case("T2-GW-05")
async def test_reconnect_skips_frames_through_last_event_id() -> None:
    async with open_gateway(script=_HELLO) as gateway:
        await gateway.create_session("s-05")
        async with listening(gateway, "s-05") as reader:
            await reader.until("ActiveRequests")
            await _reply(gateway, "s-05", "r1", "Say hello")
            live = await reader.until("TurnComplete")
            await wait_turn(gateway, "s-05")
        original = [frame["id"] for frame in live if isinstance(frame.get("id"), str)]
        assert "1" in original and "2" in original
        async with listening(gateway, "s-05", last_event_id="2") as reader:
            replayed: list[str] = []
            active: dict[str, Any] | None = None
            while active is None:
                body = payload(await reader.next_frame())
                if body.get("type") == "ActiveRequests":
                    active = body
                    break
                assert isinstance(body.get("id"), str)
                replayed.append(body["id"])
        assert active is not None
        assert "id" not in active
        assert active.get("request_ids") == []
        assert replayed == [item for item in original if int(item) > 2]
        assert "1" not in replayed and "2" not in replayed


@pytest.mark.case("T2-GW-08")
async def test_session_b_never_sees_session_a_request_id() -> None:
    async with open_gateway(script=_HELLO) as gateway:
        await gateway.create_session("s-08-a")
        await gateway.create_session("s-08-b")
        async with (
            listening(gateway, "s-08-a") as reader_a,
            listening(gateway, "s-08-b") as reader_b,
        ):
            await reader_a.until("ActiveRequests")
            await reader_b.until("ActiveRequests")
            await _reply(gateway, "s-08-a", "req-a", "Say hello")
            frames_a = await reader_a.until("TurnComplete")
            frames_b = await reader_b.collect(0.4)
            await wait_turn(gateway, "s-08-a")
        assert any(frame.get("request_id") == "req-a" for frame in frames_a)
        assert "req-a" not in repr(frames_b)


@pytest.mark.case("T2-GW-09")
async def test_cancel_during_confirmation_stales_the_confirm() -> None:
    async with open_gateway(script=_LOOP_CONFIRM) as gateway:
        await gateway.create_session("s-09")
        async with listening(gateway, "s-09") as reader:
            await reader.until("ActiveRequests")
            await _reply(gateway, "s-09", "r1", "start it")
            asked = await reader.until("ToolConfirmationRequest")
            call_id = str(_frame(asked, "ToolConfirmationRequest").get("tool_call_id"))
            cancelled = await gateway.client.post(
                "/sessions/s-09/cancel",
                json={"request_id": "r1"},
            )
            assert cancelled.status_code == 200
            terminal = await reader.until("TurnComplete")
            assert terminal[-1]["type"] == "TurnComplete"
            await wait_turn(gateway, "s-09")
            confirm = await gateway.client.post(
                f"/sessions/s-09/tool-confirmations/{call_id}",
                json={"approved": True},
            )
            assert confirm.status_code == 409
            assert error_code(confirm) == "STALE_PENDING_RESPONSE"


@pytest.mark.case("T2-GW-10")
async def test_steer_accepts_then_rejects_idle_and_full() -> None:
    async with open_gateway(script=_ASK, env={"MONKEYBOT_STEER_QUEUE_MAX": "1"}) as gateway:
        await gateway.create_session("s-10")
        async with listening(gateway, "s-10") as reader:
            await reader.until("ActiveRequests")
            await _reply(gateway, "s-10", "r1", "ask")
            await reader.until("AskUserRequest")
            accepted = await gateway.client.post(
                "/sessions/s-10/steer",
                json={"message": "steer-now"},
            )
            assert accepted.status_code == 202
            assert accepted.json()["queue"] == "steer"
            assert accepted.json()["request_id"] == "r1"
            queued = await reader.until("QueuedInputAccepted")
            event = _frame(queued, "QueuedInputAccepted")
            assert event.get("queue") == "steer"
            assert event.get("request_id") == "r1"
            full = await gateway.client.post(
                "/sessions/s-10/steer",
                json={"message": "overflow"},
            )
            assert full.status_code == 429
            assert error_code(full) == "STEER_QUEUE_FULL"
            await _answer(gateway, "s-10", "ask1", "yes")
            finished = await reader.until("TurnComplete")
            steered = _frame(finished, "UserSteered")
            assert steered.get("request_id") == "r1"
            assert "steer-now" in str(steered.get("text", ""))
            await wait_turn(gateway, "s-10")
            idle = await gateway.client.post(
                "/sessions/s-10/steer",
                json={"message": "too late"},
            )
            assert idle.status_code == 409
            assert error_code(idle) == "SESSION_IDLE"


@pytest.mark.case("T2-GW-11")
async def test_queue_starts_promotes_and_drops() -> None:
    async with open_gateway(script=_QUEUE) as gateway:
        await gateway.create_session("s-11")
        async with listening(gateway, "s-11") as reader:
            await reader.until("ActiveRequests")
            started = await gateway.client.post(
                "/sessions/s-11/queue",
                json={"request_id": "q0", "message": "go"},
            )
            assert started.status_code == 202
            assert started.json()["position"] == 0
            assert started.json()["queue"] == "follow_up"
            opening = await reader.until("TurnComplete")
            assert any(frame.get("delta") == "started-now" for frame in opening)
            await wait_turn(gateway, "s-11")
            await _reply(gateway, "s-11", "r-busy", "ask")
            await reader.until("AskUserRequest")
            queued = await gateway.client.post(
                "/sessions/s-11/queue",
                json={"request_id": "q-promote", "message": "promoted-text"},
            )
            assert queued.status_code == 202
            assert queued.json()["queue"] == "follow_up"
            assert queued.json()["request_id"] == "q-promote"
            promoted = await gateway.client.post("/sessions/s-11/queue/q-promote/steer")
            assert promoted.status_code == 202
            assert promoted.json()["queue"] == "steer"
            assert promoted.json()["request_id"] == "r-busy"
            dropped = await gateway.client.post(
                "/sessions/s-11/queue",
                json={"request_id": "q-drop", "message": "dropped-text"},
            )
            assert dropped.status_code == 202
            removed = await gateway.client.delete("/sessions/s-11/queue/q-drop")
            assert removed.status_code == 204
            await _answer(gateway, "s-11", "ask1", "yes")
            finished = await reader.until("TurnComplete")
            steered = _frame(finished, "UserSteered")
            assert steered.get("queued_request_id") == "q-promote"
            assert steered.get("request_id") == "r-busy"
            assert "promoted-text" in str(steered.get("text", ""))
            await wait_turn(gateway, "s-11")
        rows = await history(gateway, "s-11")
        blob = "\n".join(message_text(row) for row in rows)
        assert "dropped-text" not in blob
        assert "promoted-text" in blob


@pytest.mark.case("T2-GW-12")
async def test_ask_user_resumes_and_rejects_empty_answer() -> None:
    async with open_gateway(script=_ASK) as gateway:
        await gateway.create_session("s-12")
        async with listening(gateway, "s-12") as reader:
            await reader.until("ActiveRequests")
            await _reply(gateway, "s-12", "r1", "ask")
            asked = await reader.until("AskUserRequest")
            call_id = str(_frame(asked, "AskUserRequest").get("tool_call_id"))
            empty = await gateway.client.post(
                f"/sessions/s-12/ask-user/{call_id}",
                json={"answer": "   "},
            )
            assert empty.status_code == 400
            assert error_code(empty) == "EMPTY_ANSWER"
            await _answer(gateway, "s-12", call_id, "Ada")
            finished = await reader.until("TurnComplete")
            assert any("resumed" in str(frame.get("delta", "")) for frame in finished)
            await wait_turn(gateway, "s-12")


@pytest.mark.case("T2-GW-13")
async def test_elicitation_and_frontend_tool_round_trips() -> None:
    """SSE routes resolve the events the realtime loop publishes on this bus."""
    async with open_gateway(script=_HELLO) as gateway:
        await gateway.create_session("s-13")
        bus = gateway.app.state.registry.get("s-13")
        assert bus is not None
        elicitation = bus.register_pending("el-1")
        frontend = bus.register_pending("ft-1")
        async with listening(gateway, "s-13") as reader:
            await reader.until("ActiveRequests")
            await bus.publish_data(
                event_to_json(
                    ActionRequiredEvent(
                        request_id="r-el",
                        action_type="elicitation",
                        id="el-1",
                        payload={"message": "need a name"},
                    )
                )
            )
            seen = await reader.until("ActionRequiredEvent")
            event = _frame(seen, "ActionRequiredEvent")
            assert event.get("action_type") == "elicitation"
            assert event.get("payload") == {"message": "need a name"}
            posted = await gateway.client.post(
                "/sessions/s-13/elicitations/el-1",
                json={"user_data": {"name": "ada"}},
            )
            assert posted.status_code == 202
            assert elicitation.result()["user_data"]["name"] == "ada"
            await bus.publish_data(
                event_to_json(
                    FrontendToolRequestEvent(
                        request_id="r-ft",
                        tool_call_id="ft-1",
                        name="client_echo",
                        args={"text": "ping"},
                    )
                )
            )
            tool_frames = await reader.until("FrontendToolRequest")
            tool = _frame(tool_frames, "FrontendToolRequest")
            assert tool.get("tool_call_id") == "ft-1"
            result = await gateway.client.post(
                "/sessions/s-13/frontend-tool-results/ft-1",
                json={"result": [{"type": "text", "text": "pong"}], "is_error": False},
            )
            assert result.status_code == 202
            resolved = frontend.result()
            assert resolved["is_error"] is False
        unknown = await gateway.client.post(
            "/sessions/s-13/elicitations/missing",
            json={"user_data": {}},
        )
        assert unknown.status_code == 404
        assert error_code(unknown) == "PENDING_UNKNOWN"


@pytest.mark.case("T2-GW-14")
async def test_history_rewritten_precedes_replay_and_voice_excludes_it() -> None:
    from monkeybot.core.config.realtime_config import get_realtime_config

    async with open_gateway(script=_TOOL_THEN_TEXT) as gateway:
        await gateway.create_session("s-14")
        async with listening(gateway, "s-14") as reader:
            await reader.until("ActiveRequests")
            frames = await _turn(gateway, reader, "s-14", "r1", "use the tool")
            assert any(frame.get("type") == "ToolCallResult" for frame in frames)
        anchor = user_row_id(await history(gateway, "s-14"), "use the tool")
        manager = RealtimeSessionManager(config=get_realtime_config())
        gateway.app.state.realtime_manager = manager
        assert manager.claim("s-14")
        blocked = await gateway.client.post(
            "/sessions/s-14/branches",
            json={"op": "regenerate", "anchor": {"row_id": anchor}, "request_id": "replay-1"},
        )
        assert blocked.status_code == 409
        assert error_code(blocked) == "SESSION_BUSY"
        manager.unclaim("s-14")
        async with listening(gateway, "s-14") as reader:
            await reader.until("ActiveRequests")
            started = await gateway.client.post(
                "/sessions/s-14/branches",
                json={
                    "op": "regenerate",
                    "anchor": {"row_id": anchor},
                    "request_id": "replay-1",
                },
            )
            assert started.status_code == 200
            replay = await reader.until("TurnComplete")
            await wait_turn(gateway, "s-14")
        rewritten = _frame(replay, "HistoryRewritten")
        delta = _frame(replay, "AssistantDelta", request_id="replay-1")
        assert int(str(rewritten["id"])) < int(str(delta["id"]))
        assert rewritten.get("op") == "regenerate"


@pytest.mark.case("T2-GW-15")
async def test_branch_routes_behaviors_and_busy_409s() -> None:
    async with open_gateway(script=_ASK) as busy:
        await busy.create_session("s-15-busy")
        async with listening(busy, "s-15-busy") as reader:
            await reader.until("ActiveRequests")
            await _reply(busy, "s-15-busy", "r1", "ask")
            await reader.until("AskUserRequest")
            anchor = {"row_id": "x"}
            edit = await busy.client.post(
                "/sessions/s-15-busy/branches",
                json={"op": "edit", "anchor": anchor, "message": "changed"},
            )
            regenerate = await busy.client.post(
                "/sessions/s-15-busy/branches",
                json={"op": "regenerate", "anchor": anchor},
            )
            restore = await busy.client.post(
                "/sessions/s-15-busy/branches",
                json={"op": "restore", "anchor": anchor},
            )
            switch = await busy.client.put(
                "/sessions/s-15-busy/branches/active",
                json={"branch_id": "missing"},
            )
            truncate = await busy.client.post(
                "/sessions/s-15-busy/truncate",
                json={"anchor": anchor},
            )
            fork = await busy.client.post(
                "/sessions/s-15-busy/fork",
                json={"anchor": anchor},
            )
            for response in (edit, regenerate, restore, switch, truncate, fork):
                assert response.status_code == 409
                assert error_code(response) == "SESSION_BUSY"
            await _answer(busy, "s-15-busy", "ask1", "yes")
            await reader.until("TurnComplete")
            await wait_turn(busy, "s-15-busy")

    async with open_gateway(script=_BRANCHES) as gateway:
        session = "s-15"
        await gateway.create_session(session)
        async with listening(gateway, session) as reader:
            await reader.until("ActiveRequests")
            await _turn(gateway, reader, session, "r1", "first user")
            await _turn(gateway, reader, session, "r2", "second user")
            rows = await history(gateway, session)
            first = user_row_id(rows, "first user")
            second = user_row_id(rows, "second user")
            bare_edit = await gateway.client.post(
                f"/sessions/{session}/branches",
                json={"op": "edit", "anchor": {"row_id": first}},
            )
            assert bare_edit.status_code == 400
            noisy_restore = await gateway.client.post(
                f"/sessions/{session}/branches",
                json={"op": "restore", "anchor": {"row_id": second}, "message": "nope"},
            )
            assert noisy_restore.status_code == 400
            missing = await gateway.client.put(
                f"/sessions/{session}/branches/active",
                json={"branch_id": "missing-branch"},
            )
            assert missing.status_code == 404
            assert error_code(missing) == "BRANCH_NOT_FOUND"
            truncated = await gateway.client.post(
                f"/sessions/{session}/truncate",
                json={"anchor": {"row_id": first}},
            )
            assert truncated.status_code == 200
            truncate_frames = await reader.until("HistoryRewritten")
            assert _frame(truncate_frames, "HistoryRewritten").get("op") == "truncate"
            await wait_turn(gateway, session)
            await _turn(gateway, reader, session, "r3", "second user")
            rows = await history(gateway, session)
            second = user_row_id(rows, "second user")
            first = user_row_id(rows, "first user")
            restored = await gateway.client.post(
                f"/sessions/{session}/branches",
                json={"op": "restore", "anchor": {"row_id": second}},
            )
            assert restored.status_code == 200
            restore_frames = await reader.until("HistoryRewritten")
            assert _frame(restore_frames, "HistoryRewritten").get("op") == "restore"
            await wait_turn(gateway, session)
            regenerated = await gateway.client.post(
                f"/sessions/{session}/branches",
                json={
                    "op": "regenerate",
                    "anchor": {"row_id": first},
                    "request_id": "regen-1",
                },
            )
            assert regenerated.status_code == 200
            regen_frames = await reader.until("TurnComplete")
            assert _frame(regen_frames, "HistoryRewritten").get("op") == "regenerate"
            await wait_turn(gateway, session)
            current = user_row_id(await history(gateway, session), "first user")
            edited = await gateway.client.post(
                f"/sessions/{session}/branches",
                json={
                    "op": "edit",
                    "anchor": {"row_id": current},
                    "message": "edited user",
                    "request_id": "edit-1",
                },
            )
            assert edited.status_code == 200
            edit_frames = await reader.until("TurnComplete")
            assert _frame(edit_frames, "HistoryRewritten").get("op") == "edit"
            assert any(frame.get("delta") == "edited" for frame in edit_frames)
            await wait_turn(gateway, session)
            listed = await gateway.client.get(f"/sessions/{session}/branches")
            assert listed.status_code == 200
            body = listed.json()
            others = [
                item["branch_id"]
                for item in body["branches"]
                if item["branch_id"] != body["active_branch_id"]
            ]
            assert others
            switched = await gateway.client.put(
                f"/sessions/{session}/branches/active",
                json={"branch_id": others[0]},
            )
            assert switched.status_code == 200
            switch_frames = await reader.until("HistoryRewritten")
            assert _frame(switch_frames, "HistoryRewritten").get("op") == "switch"
            await wait_turn(gateway, session)
            kept = user_row_id(await history(gateway, session), "first user")
            forked = await gateway.client.post(
                f"/sessions/{session}/fork",
                json={"anchor": {"row_id": kept}},
            )
            assert forked.status_code == 200
            fork_id = forked.json()["session_id"]
            assert gateway.app.state.registry.get(fork_id) is not None
            fork_rows = await history(gateway, fork_id)
            assert any("first user" in message_text(row) for row in fork_rows)


@pytest.mark.case("T2-GW-16")
async def test_delete_session_quiesces_and_is_idempotent() -> None:
    async with open_gateway(script=_ASK) as gateway:
        await gateway.create_session("s-16")
        async with listening(gateway, "s-16") as reader:
            await reader.until("ActiveRequests")
            await _reply(gateway, "s-16", "r1", "ask")
            await reader.until("AskUserRequest")
            deleted = await gateway.client.delete("/sessions/s-16")
            assert deleted.status_code == 200
            assert deleted.json()["deleted"] is True
            terminal = await reader.until("TurnComplete")
            assert terminal[-1]["type"] == "TurnComplete"
        again = await gateway.client.delete("/sessions/s-16")
        assert again.status_code == 200
        assert again.json()["deleted"] is False


@pytest.mark.case("T2-GW-17")
async def test_usage_routes_match_recorded_rows() -> None:
    async with open_gateway(script=_USAGE) as gateway:
        await gateway.create_session("s-17")
        async with listening(gateway, "s-17") as reader:
            await reader.until("ActiveRequests")
            frames = await _turn(gateway, reader, "s-17", "r1", "count")
        complete = _frame(frames, "TurnComplete")
        store = gateway.app.state.storage.usage()
        recorded = await store.summary(thread_id="s-17")
        session = await gateway.client.get("/sessions/s-17/usage")
        assert session.status_code == 200
        body = session.json()
        assert body["turns"] == recorded.turns
        assert body["input_tokens"] == recorded.input_tokens
        assert body["output_tokens"] == recorded.output_tokens
        assert body["input_tokens"] == complete["usage"]["input_tokens"]
        assert body["output_tokens"] == complete["usage"]["output_tokens"]
        assert recorded.turns >= 1
        now = int(time.time() * 1000)
        missing = await gateway.client.get("/usage", params={"bucket": "hour"})
        assert missing.status_code == 400
        wide = await gateway.client.get(
            "/usage",
            params={"bucket": "hour", "since": str(now - 8 * 24 * 60 * 60 * 1000)},
        )
        assert wide.status_code == 400
        hour = await gateway.client.get(
            "/usage",
            params={"bucket": "hour", "since": str(now - 60_000)},
        )
        assert hour.status_code == 200
        agent = await store.summary(thread_id=None)
        assert hour.json()["input_tokens"] == agent.input_tokens
        assert hour.json()["output_tokens"] == agent.output_tokens
        day = await gateway.client.get("/usage", params={"bucket": "day"})
        assert day.status_code == 200
        assert day.json()["input_tokens"] == agent.input_tokens
        assert day.json()["turns"] == agent.turns


@pytest.mark.case("T2-GW-18")
async def test_workspace_and_history_toggles_and_path_confinement() -> None:
    off = {
        "MONKEYBOT_WORKSPACE_API": "0",
        "MONKEYBOT_CHAT_HISTORY_API": "0",
    }
    async with open_gateway(script=_HELLO, env=off) as gateway:
        tree = await gateway.client.get("/api/workspace/tree")
        file_off = await gateway.client.get("/api/workspace/file", params={"path": "notes.txt"})
        history_off = await gateway.client.get("/api/chat-history")
        for response in (tree, file_off, history_off):
            assert response.status_code == 404
            assert error_code(response) == "NOT_FOUND"
    async with open_gateway(script=_HELLO) as gateway:
        notes = gateway.agent_root / "workspace" / "notes.txt"
        notes.write_text("kept\n", encoding="utf-8")
        allowed = await gateway.client.get("/api/workspace/file", params={"path": "notes.txt"})
        assert allowed.status_code == 200
        assert "kept" in allowed.json()["content"]
        escaped = await gateway.client.get(
            "/api/workspace/file",
            params={"path": "../monkeybot_config/monkeybot.yaml"},
        )
        assert escaped.status_code == 400
        assert error_code(escaped) == "BAD_REQUEST"
        history_on = await gateway.client.get("/api/chat-history")
        assert history_on.status_code == 200


@pytest.mark.case("T2-GW-19")
async def test_chat_title_is_clipped_and_model_failure_is_null() -> None:
    async with open_gateway(script=_TITLE) as gateway:
        await gateway.create_session("s-19")
        async with listening(gateway, "s-19") as reader:
            await reader.until("ActiveRequests")
            await _turn(gateway, reader, "s-19", "r1", "name this")
        titled = await gateway.client.post("/api/chat-title", json={"session_id": "s-19"})
        assert titled.status_code == 200
        title = titled.json()["title"]
        assert isinstance(title, str)
        assert title
        assert len(title) <= 60
    async with open_gateway(script=_TITLE_FAIL) as gateway:
        await gateway.create_session("s-19b")
        async with listening(gateway, "s-19b") as reader:
            await reader.until("ActiveRequests")
            await _turn(gateway, reader, "s-19b", "r1", "name this")
        failed = await gateway.client.post("/api/chat-title", json={"session_id": "s-19b"})
        assert failed.status_code == 200
        assert failed.json()["title"] is None


@pytest.mark.case("T2-GW-20")
async def test_health_shape_includes_memory_detail() -> None:
    async with open_gateway(script=_HELLO) as gateway:
        health = await gateway.client.get("/health")
        assert health.status_code == 200
        body = health.json()
        assert body["status"] == "ok"
        assert body["version"] == "2.0.0"
        assert body["memory"] == "disabled"
        assert body["memory_detail"] == "memory.enabled=false"


@pytest.mark.case("T2-ATT-G1")
async def test_upload_then_reply_emits_attachment_descriptor() -> None:
    async with open_gateway(script=_HELLO, env={"ATTACHMENTS_ENABLED": "true"}) as gateway:
        await gateway.create_session("s-att")
        uploaded = await gateway.client.post(
            "/sessions/s-att/attachments",
            files={"file": ("hello.txt", b"hello\n", "text/plain")},
        )
        assert uploaded.status_code == 201
        attachment_id = uploaded.json()["attachment_id"]
        async with listening(gateway, "s-att") as reader:
            await reader.until("ActiveRequests")
            response = await gateway.client.post(
                "/sessions/s-att/reply",
                json={
                    "request_id": "r1",
                    "content": [
                        {
                            "type": "attachmentRef",
                            "attachmentId": attachment_id,
                            "mimeType": "text/plain",
                        }
                    ],
                },
            )
            assert response.status_code == 200
            frames = await reader.until("TurnComplete")
            await wait_turn(gateway, "s-att")
        descriptor = _frame(frames, "AttachmentDescriptor")
        assert descriptor.get("attachment_id") == attachment_id
        assert frames[-1]["type"] == "TurnComplete"


@pytest.mark.case("T2-ATT-G2")
async def test_upload_limits_and_reply_attachment_errors() -> None:
    async with open_gateway(script=_HELLO) as gateway:
        await gateway.create_session("s-off")
        disabled = await gateway.client.post(
            "/sessions/s-off/attachments",
            files={"file": ("hello.txt", b"hello\n", "text/plain")},
        )
        assert disabled.status_code == 404
        assert error_code(disabled) == "NOT_FOUND"
    env = {"ATTACHMENTS_ENABLED": "true", "ATTACHMENT_MAX_IMAGE_BYTES": "8"}
    async with open_gateway(script=_HELLO, env=env) as gateway:
        await gateway.create_session("s-lim")
        oversize = await gateway.client.post(
            "/sessions/s-lim/attachments",
            files={"file": ("big.png", b"\x89PNG\r\n\x1a\n" + b"x" * 20, "image/png")},
        )
        assert oversize.status_code == 413
        assert error_code(oversize) == "PAYLOAD_TOO_LARGE"
        bad_type = await gateway.client.post(
            "/sessions/s-lim/attachments",
            files={"file": ("notes.zip", b"PK\x03\x04not-a-real-zip", "application/zip")},
        )
        assert bad_type.status_code == 415
        assert error_code(bad_type) == "UNSUPPORTED_MEDIA_TYPE"
        # A rejected /reply keeps the turn lock, so each body error uses its own session.
        body_errors = (
            (
                "s-inline",
                {
                    "request_id": "r-inline",
                    "content": [{"type": "image", "mimeType": "image/png", "data": "aaaa"}],
                },
                400,
                "INLINE_ATTACHMENT_NOT_ALLOWED",
            ),
            (
                "s-both",
                {
                    "request_id": "r-both",
                    "message": "hi",
                    "content": [{"type": "text", "text": "hi"}],
                },
                400,
                "AMBIGUOUS_REPLY_BODY",
            ),
            (
                "s-missing",
                {
                    "request_id": "r-missing",
                    "content": [
                        {
                            "type": "attachmentRef",
                            "attachmentId": "missing",
                            "mimeType": "text/plain",
                        }
                    ],
                },
                404,
                "ATTACHMENT_NOT_FOUND",
            ),
        )
        for session_id, body, status, code in body_errors:
            await gateway.create_session(session_id)
            response = await gateway.client.post(f"/sessions/{session_id}/reply", json=body)
            assert response.status_code == status
            assert error_code(response) == code


@pytest.mark.case("T2-SCHED-G1")
async def test_invoke_tick_during_turn_is_409_and_loop_stays_active() -> None:
    async with open_gateway(script=_ASK) as gateway:
        await gateway.create_session("s-tick")
        created = await gateway.client.post(
            "/scheduler/loops",
            json={
                "prompt": "tick",
                "interval": "1h",
                "session_id": "s-tick",
                "max_ticks": 2,
                "confirmed": True,
            },
        )
        assert created.status_code == 201
        loop_id = created.json()["loop"]["loop_id"]
        assert created.json()["loop"]["status"] == "active"
        async with listening(gateway, "s-tick") as reader:
            await reader.until("ActiveRequests")
            await _reply(gateway, "s-tick", "r1", "ask")
            await reader.until("AskUserRequest")
            tick = await gateway.client.post(
                "/scheduler/invoke-tick",
                json={"session_id": "s-tick", "request_id": "tick-1", "message": "run"},
            )
            assert tick.status_code == 409
            assert error_code(tick) == "SESSION_BUSY"
            current = await gateway.client.get(f"/scheduler/loops/{loop_id}")
            assert current.status_code == 200
            assert current.json()["loop"]["status"] == "active"
            await _answer(gateway, "s-tick", "ask1", "yes")
            await reader.until("TurnComplete")
            await wait_turn(gateway, "s-tick")


@pytest.mark.case("T2-SCHED-G2")
async def test_scheduler_and_goal_lifecycle() -> None:
    async with open_gateway(script=_HELLO) as gateway:
        unconfirmed = await gateway.client.post(
            "/scheduler/loops",
            json={
                "prompt": "tick",
                "interval": "1h",
                "session_id": "s-loop",
                "max_ticks": 1,
                "confirmed": False,
            },
        )
        assert unconfirmed.status_code == 400
        assert error_code(unconfirmed) == "CONFIRMATION_REQUIRED"
        reserved = await gateway.client.post(
            "/scheduler/loops",
            json={
                "prompt": "tick",
                "interval": "1h",
                "session_id": "subagent:nope",
                "max_ticks": 1,
                "confirmed": True,
            },
        )
        assert reserved.status_code == 400
        assert error_code(reserved) == "BAD_REQUEST"
        created = await gateway.client.post(
            "/scheduler/loops",
            json={
                "prompt": "tick",
                "interval": "1h",
                "session_id": "s-loop",
                "max_ticks": 2,
                "confirmed": True,
            },
        )
        assert created.status_code == 201
        loop_id = created.json()["loop"]["loop_id"]
        listed = await gateway.client.get("/scheduler/loops")
        assert any(item["loop_id"] == loop_id for item in listed.json()["loops"])
        fetched = await gateway.client.get(f"/scheduler/loops/{loop_id}")
        assert fetched.status_code == 200
        assert fetched.json()["loop"]["status"] == "active"
        paused = await gateway.client.post(f"/scheduler/loops/{loop_id}/pause")
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
        resumed = await gateway.client.post(f"/scheduler/loops/{loop_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "active"
        stopped = await gateway.client.post(f"/scheduler/loops/{loop_id}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "completed"
        service = DurableGoalService(gateway.app.state.storage.scheduled_loops())
        goal, created_goal = await service.create(
            objective="ship the catalog", session_id="goal-host"
        )
        assert created_goal
        goal_id = goal.loop_id
        fetched_goal = await gateway.client.get(f"/goals/{goal_id}")
        assert fetched_goal.status_code == 200
        assert fetched_goal.json()["goal"]["status"] == "active"
        goal_pause = await gateway.client.post(f"/goals/{goal_id}/pause")
        assert goal_pause.json()["goal"]["status"] == "paused"
        goal_resume = await gateway.client.post(f"/goals/{goal_id}/resume")
        assert goal_resume.json()["goal"]["status"] == "active"
        goals = await gateway.client.get("/goals")
        assert any(item["id"] == goal_id for item in goals.json()["goals"])
        goal_stop = await gateway.client.post(f"/goals/{goal_id}/stop")
        assert goal_stop.status_code == 200
        assert goal_stop.json()["goal"]["status"] == "completed"


@pytest.mark.case("T2-STORE-G1")
async def test_regenerate_replays_without_splitting_tool_pairs() -> None:
    async with open_gateway(script=_TOOL_PAIR_REPLAY) as gateway:
        await gateway.create_session("s-store")
        async with listening(gateway, "s-store") as reader:
            await reader.until("ActiveRequests")
            await _turn(gateway, reader, "s-store", "r1", "use the tool")
            anchor = user_row_id(await history(gateway, "s-store"), "use the tool")
            started = await gateway.client.post(
                "/sessions/s-store/branches",
                json={
                    "op": "regenerate",
                    "anchor": {"row_id": anchor},
                    "request_id": "replay-1",
                },
            )
            assert started.status_code == 200
            replay = await reader.until("TurnComplete")
            await wait_turn(gateway, "s-store")
        rewritten = _frame(replay, "HistoryRewritten")
        delta = _frame(replay, "AssistantDelta", request_id="replay-1")
        assert rewritten.get("op") == "regenerate"
        assert int(str(rewritten["id"])) < int(str(delta["id"]))
        _paired(await history(gateway, "s-store"))


@pytest.mark.case("T2-CFG-G1")
async def test_reload_hot_model_name_reaches_every_session() -> None:
    async with open_gateway(script=_HELLO) as gateway:
        await gateway.create_session("s-hot-a")
        await gateway.create_session("s-hot-b")
        async with (
            listening(gateway, "s-hot-a") as reader_a,
            listening(gateway, "s-hot-b") as reader_b,
        ):
            await reader_a.until("ActiveRequests")
            await reader_b.until("ActiveRequests")
            _rewrite_config(gateway, _set_model_name("renamed-model"))
            reloaded = await gateway.client.post("/admin/config/reload", json={})
            assert reloaded.status_code == 200
            assert "MODEL_NAME" in reloaded.json()["hot"]
            frames_a = await reader_a.until("ConfigReloaded")
            frames_b = await reader_b.until("ConfigReloaded")
            assert _frame(frames_a, "ConfigReloaded")
            assert _frame(frames_b, "ConfigReloaded")
            assert "MODEL_NAME" in _frame(frames_a, "ConfigReloaded").get("hot", [])
            original = gateway_runtime.provider
            recorder = _RecordingProvider(original)
            gateway_runtime.provider = recorder
            try:
                await _turn(gateway, reader_a, "s-hot-a", "r1", "Say hello")
            finally:
                gateway_runtime.provider = original
        assert recorder.models
        assert recorder.models[-1] == "renamed-model"


@pytest.mark.case("T2-CFG-G2")
async def test_reload_db_url_is_restart_required_and_backend_stays() -> None:
    # DB_URL in the process environment wins over paths.db_url, so this case
    # leaves it unset and changes the file value that is actually in force.
    async with open_gateway(
        script=_HELLO,
        config={"paths": {"db_url": "sqlite:///data/monkeybot.db"}},
        db_from_config=True,
    ) as gateway:
        storage = gateway.app.state.storage
        _rewrite_config(gateway, _set_db_url("sqlite:///data/other.db"))
        reloaded = await gateway.client.post("/admin/config/reload", json={})
        assert reloaded.status_code == 200
        body = reloaded.json()
        assert "DB_URL" in body["restart_required"]
        assert gateway.app.state.storage is storage


@pytest.mark.case("T2-CFG-G3")
async def test_reload_during_turn_keeps_the_pinned_model() -> None:
    async with open_gateway(script=_ASK) as gateway:
        await gateway.create_session("s-pin")
        original = gateway_runtime.provider
        recorder = _RecordingProvider(original)
        gateway_runtime.provider = recorder
        try:
            async with listening(gateway, "s-pin") as reader:
                await reader.until("ActiveRequests")
                await _reply(gateway, "s-pin", "r1", "ask")
                await reader.until("AskUserRequest")
                assert recorder.models == ["fake"]
                _rewrite_config(gateway, _set_model_name("renamed-model"))
                reloaded = await gateway.client.post("/admin/config/reload", json={})
                assert reloaded.status_code == 200
                await _answer(gateway, "s-pin", "ask1", "yes")
                await reader.until("TurnComplete")
                await wait_turn(gateway, "s-pin")
                assert recorder.models == ["fake", "fake"]
                await _turn(gateway, reader, "s-pin", "r2", "next")
        finally:
            gateway_runtime.provider = original
    assert recorder.models[-1] == "renamed-model"
    assert recorder.models[:-1] == ["fake", "fake"]


@pytest.mark.case("T2-CFG-G4")
async def test_reload_rejects_remote_clients_and_requires_bearer() -> None:
    token = "p204-admin"
    async with open_gateway(script=_HELLO, env={"MONKEYBOT_ADMIN_TOKEN": token}) as gateway:
        bearer = {"authorization": f"Bearer {token}"}
        async with remote_client(gateway, "10.1.2.3") as remote:
            denied = await remote.post(
                "/admin/config/reload",
                json={"env": {"MODEL_NAME": "remote-model"}},
                headers=bearer,
            )
        assert denied.status_code == 403
        assert error_code(denied) == "FORBIDDEN"
        missing = await gateway.client.post(
            "/admin/config/reload",
            json={"env": {"MODEL_NAME": "no-token"}},
        )
        assert missing.status_code == 401
        assert error_code(missing) == "UNAUTHORIZED"
        allowed = await gateway.client.post(
            "/admin/config/reload",
            json={"env": {"MODEL_NAME": "loopback-model"}},
            headers=bearer,
        )
        assert allowed.status_code == 200


@pytest.mark.case("T2-CFG-G5")
async def test_ordinary_memory_setup_failure_leaves_gateway_ready() -> None:
    descriptor, palace_file = tempfile.mkstemp(prefix="mb-palace-file-")
    os.close(descriptor)
    try:
        env = {
            "MONKEYBOT_MEMORY_HOOK_ENABLED": "true",
            "MEMORY_STORAGE_URI": palace_file,
        }
        async with open_gateway(script=_HELLO, env=env) as gateway:
            health = await gateway.client.get("/health")
            assert health.status_code == 200
            body = health.json()
            assert body["status"] == "ok"
            assert body["memory"] == "unavailable"
            assert isinstance(body["memory_detail"], str) and body["memory_detail"]
            created = await gateway.client.post("/sessions", json={"session_id": "still-up"})
            assert created.status_code == 201
    finally:
        os.unlink(palace_file)


@pytest.mark.case("T2-CFG-G5")
async def test_memory_configuration_error_aborts_startup() -> None:
    try:
        async with open_gateway(
            script=_HELLO,
            config={"paths": {"auto_schema": False}, "memory": {"enabled": True}},
            env={"MONKEYBOT_MEMORY_HOOK_ENABLED": "true"},
        ):
            raise AssertionError("gateway started after a memory configuration error")
    except AssertionError:
        raise
    except BaseException as exc:
        assert _chain_has(exc, MemoryConfigurationError)
    else:
        raise AssertionError("startup did not fail")
