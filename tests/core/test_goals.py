"""Native durable goal lifecycle over scheduled-loop persistence."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient

from monkeybot.core.context import GOAL_TOOL_DEFS, GOAL_TOOL_NAMES, SkillRef, build_context
from monkeybot.core.goals.service import DurableGoalService, GoalConflictError
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.persistence.scheduled_loops import (
    KIND_GOAL,
    KIND_LOOP,
    ScheduledLoopCreate,
    ScheduledLoopRow,
    format_tick_prompt,
    resolve_complete_tick,
)
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.inspector import InspectorToolCall
from monkeybot.core.tools.loop_inspector import LoopStartInspector
from monkeybot.gateway.sse.routes import create_app
from monkeybot.gateway.sse.session_bus import SessionRegistry
from tests.core.test_context import FakeMCPClient
from tests.core.test_core_tool_executor import _ctx, _mem_sub, _NoMCP


@pytest.fixture
async def store(tmp_path):
    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path / 'goals.db'}")
    await backend.open()
    yield backend.scheduled_loops()
    await backend.close()


async def _force_due(store, loop_id: str) -> None:
    await store._conn.execute(
        "UPDATE scheduled_loops SET next_tick_at_ms = 1 WHERE loop_id = ?",
        (loop_id,),
    )
    await store._conn.commit()


@pytest.mark.asyncio
async def test_goal_create_is_idempotent_and_one_per_session(store) -> None:
    service = DurableGoalService(store)
    row, created = await service.create(objective="Ship the report", session_id="sess-1")
    assert created is True
    assert row.kind == KIND_GOAL
    assert row.max_ticks is None
    assert row.max_runtime_ms is None
    assert row.next_tick_at_ms > row.started_at_ms
    again, created_again = await service.create(
        objective="Ship the report",
        session_id="sess-1",
    )
    assert created_again is False
    assert again.loop_id == row.loop_id
    with pytest.raises(GoalConflictError):
        await service.create(objective="A different objective", session_id="sess-1")
    due = await store.list_due(row.started_at_ms)
    assert all(item.loop_id != row.loop_id for item in due)


@pytest.mark.asyncio
async def test_goal_pause_resume_and_user_stop(store) -> None:
    service = DurableGoalService(store)
    row, _ = await service.create(objective="Ship it", session_id="sess-1")
    paused = await service.pause(row.loop_id)
    assert paused.status == "paused"
    resumed = await service.resume(row.loop_id)
    assert resumed.status == "active"
    via_tool = await service.update(session_id="sess-1", status="active")
    assert via_tool.status == "active"
    await service.pause(row.loop_id)
    via_resume = await service.update(session_id="sess-1", status="active")
    assert via_resume.status == "active"
    stopped = await service.stop(row.loop_id)
    assert stopped.status == "completed"
    assert stopped.stop_reason == "manual"
    again = await service.stop(row.loop_id)
    assert again.status == "completed"


@pytest.mark.asyncio
async def test_goal_tick_error_retries_instead_of_failing(store) -> None:
    service = DurableGoalService(store)
    row, _ = await service.create(objective="Keep going", session_id="sess-1")
    await _force_due(store, row.loop_id)
    claimed = await store.claim_tick(row.loop_id, "worker-1")
    assert claimed is not None
    completed = await store.complete_tick(row.loop_id, worker_id="worker-1", error="timeout")
    assert completed is not None
    assert completed.status == "active"
    assert completed.last_error == "timeout"
    assert completed.stop_reason is None
    await service.update(session_id="sess-1", status="complete")
    done = await store.get(row.loop_id)
    assert done is not None
    assert done.status == "completed"
    assert done.stop_reason == "complete"
    claimed_after = await store.claim_tick(row.loop_id, "worker-1")
    assert claimed_after is None


@pytest.mark.asyncio
async def test_goal_isolated_from_loop_tools_and_routes(tmp_path) -> None:
    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path / 'mix.db'}")
    await backend.open()
    store = backend.scheduled_loops()
    goals = DurableGoalService(store)
    goal, _ = await goals.create(objective="Ship it", session_id="sess-1")
    loop = await store.create(
        ScheduledLoopCreate(
            prompt="BUSINESS: poll",
            interval_ms=5000,
            session_id="loop-main",
            loop_id="demo-loop",
            max_ticks=3,
            kind=KIND_LOOP,
        )
    )
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=skills,
        mcp=_NoMCP(),
        scheduled_loop_store=store,
    )
    listed = await ex.execute(
        call=ToolCall(name="loop_status", args={}, call_id="1"),
        ctx=_ctx(),
    )
    body = json.loads(listed.blocks[0].text)  # type: ignore[index]
    ids = {item["loop_id"] for item in body["loops"]}
    assert loop.loop_id in ids
    assert goal.loop_id not in ids

    missing = await ex.execute(
        call=ToolCall(name="pause_loop", args={"loop_id": goal.loop_id}, call_id="2"),
        ctx=_ctx(),
    )
    assert missing.error is not None
    assert "unknown loop" in missing.error

    reg = SessionRegistry()

    class _NoopLoop:
        async def start_turn(self, session_id: str, request_id: str, user_content) -> None:
            del session_id, request_id, user_content

    app = create_app(registry=reg, loop_port=_NoopLoop())
    app.state.storage = backend
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        loops = await client.get("/scheduler/loops")
        assert loops.status_code == 200
        loop_ids = {row["loop_id"] for row in loops.json()["loops"]}
        assert loop.loop_id in loop_ids
        assert goal.loop_id not in loop_ids
        goal_list = await client.get("/goals")
        assert goal_list.status_code == 200
        goal_ids = {row["id"] for row in goal_list.json()["goals"]}
        assert goal.loop_id in goal_ids
        assert loop.loop_id not in goal_ids
        fetched = await client.get(f"/goals/{goal.loop_id}")
        assert fetched.status_code == 200
        assert fetched.json()["goal"]["objective"] == "Ship it"
        missing_goal = await client.get(f"/goals/{loop.loop_id}")
        assert missing_goal.status_code == 404
        paused = await client.post(f"/goals/{goal.loop_id}/pause")
        assert paused.status_code == 200
        assert paused.json()["goal"]["status"] == "paused"
        resumed = await client.post(f"/goals/{goal.loop_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["goal"]["status"] == "active"
        forbidden = await client.post(f"/scheduler/loops/{goal.loop_id}/pause")
        assert forbidden.status_code == 404
        stopped = await client.post(f"/goals/{goal.loop_id}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["goal"]["status"] == "completed"
    await backend.close()


@pytest.mark.asyncio
async def test_create_goal_requires_slash_invocation(tmp_path) -> None:
    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path / 'tool.db'}")
    await backend.open()
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=skills,
        mcp=_NoMCP(),
        scheduled_loop_store=backend.scheduled_loops(),
    )
    denied = await ex.execute(
        call=ToolCall(name="create_goal", args={"objective": "Ship it"}, call_id="1"),
        ctx=_ctx(),
    )
    assert denied.error is not None
    assert "/goal" in denied.error
    created = await ex.execute(
        call=ToolCall(name="create_goal", args={"objective": "Ship it"}, call_id="2"),
        ctx=replace(_ctx(), invoked_skill=SkillRef(name="goal", description="Goal")),
    )
    assert created.error is None
    payload = json.loads(created.blocks[0].text)  # type: ignore[index]
    assert payload["created"] is True
    paused = await DurableGoalService(backend.scheduled_loops()).pause(payload["goal"]["id"])
    assert paused.status == "paused"
    resumed = await ex.execute(
        call=ToolCall(name="update_goal", args={"status": "active"}, call_id="3"),
        ctx=replace(_ctx(), invoked_skill=SkillRef(name="goal", description="Goal")),
    )
    assert resumed.error is None
    done = await ex.execute(
        call=ToolCall(name="update_goal", args={"status": "complete"}, call_id="4"),
        ctx=_ctx(),
    )
    assert done.error is None
    body = json.loads(done.blocks[0].text)  # type: ignore[index]
    assert body["goal"]["status"] == "completed"
    await backend.close()


@pytest.mark.asyncio
async def test_create_goal_skips_loop_confirmation() -> None:
    inspector = LoopStartInspector()
    allowed = await inspector.check(
        InspectorToolCall(call_id="1", name="create_goal", args={"objective": "Ship it"}),
        _ctx(),
    )
    assert allowed.kind == "allow"
    confirm = await inspector.check(
        InspectorToolCall(
            call_id="2",
            name="start_loop",
            args={"prompt": "BUSINESS: poll", "interval": "5m"},
        ),
        _ctx(),
    )
    assert confirm.kind == "confirm"


@pytest.mark.asyncio
async def test_build_context_advertises_goal_tools_when_storage_exists(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATTACHMENTS_ENABLED", "false")
    (tmp_path / "AGENT.md").write_text("You are a helpful assistant.\n", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    ctx = await build_context(
        "t",
        "r",
        agent_md_path=tmp_path / "AGENT.md",
        memory=None,
        skills_path=tmp_path / "skills",
        mcp_client=FakeMCPClient([]),
        scheduled_loops_available=True,
        loops_advertised=False,
    )
    names = {t.name for t in ctx.tools}
    assert GOAL_TOOL_NAMES.issubset(names)
    assert "start_loop" not in names


def test_goal_tool_schemas_are_cursor_style() -> None:
    tools = {t.name: t for t in GOAL_TOOL_DEFS}
    create = tools["create_goal"]
    assert list(create.input_schema["properties"]) == ["objective"]
    assert create.input_schema["required"] == ["objective"]
    update = tools["update_goal"]
    assert list(update.input_schema["properties"]) == ["status"]
    assert update.input_schema["properties"]["status"]["enum"] == ["active", "complete"]
    assert update.input_schema["required"] == ["status"]


@pytest.mark.asyncio
async def test_goal_survives_backend_reopen(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path / 'reopen.db'}"
    backend = SQLiteStorageBackend(db_url)
    await backend.open()
    row, _ = await DurableGoalService(backend.scheduled_loops()).create(
        objective="Ship it", session_id="sess-1"
    )
    goal_id = row.loop_id
    await backend.close()
    again = SQLiteStorageBackend(db_url)
    await again.open()
    restored = await DurableGoalService(again.scheduled_loops()).get(goal_id)
    assert restored is not None
    assert restored.kind == KIND_GOAL
    assert restored.objective == "Ship it"
    assert restored.status == "active"
    await again.close()


def test_goal_tick_prompt_restores_objective() -> None:
    row = ScheduledLoopRow(
        loop_id="goal-abc",
        session_id="sess-1",
        status="active",
        prompt="Ship the weekly report",
        interval_ms=300000,
        max_ticks=None,
        max_runtime_ms=None,
        skip_if_busy=True,
        tick_index=2,
        next_tick_at_ms=1,
        started_at_ms=0,
        last_tick_at_ms=None,
        last_error=None,
        stop_reason=None,
        tick_in_flight=False,
        kind=KIND_GOAL,
        objective="Ship the weekly report",
    )
    prompt = format_tick_prompt(row)
    assert "Ship the weekly report" in prompt
    assert "Do not call create_goal again" in prompt
    assert "SCHEDULED TICK" not in prompt
    tick_index, status, stop_reason, _, last_error = resolve_complete_tick(
        row, error="boom", now_ms=1000
    )
    assert tick_index == 3
    assert status == "active"
    assert stop_reason is None
    assert last_error == "boom"
