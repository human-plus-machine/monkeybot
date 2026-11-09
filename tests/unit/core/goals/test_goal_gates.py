"""Completion audit, budget pause, wake, and waiting for native goals."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from monkeybot.core.context import SkillRef
from monkeybot.core.goals.completion import (
    AuditItem,
    ProviderCompletionJudge,
    ScriptedCompletionVerifier,
    _parse_completion_judge,
    audit_blocks_completion,
    parse_audit,
)
from monkeybot.core.goals.service import (
    DurableGoalService,
    GoalClosedError,
    GoalPausedError,
)
from monkeybot.core.llm.provider import Done, TextDelta, ToolCall
from monkeybot.core.persistence.scheduled_loops import (
    KIND_GOAL,
    OpenGoalExistsError,
    ScheduledLoopCreate,
    format_tick_prompt,
)
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from testkit.context import tool_ctx as _ctx
from testkit.mcp import NoMCP
from testkit.memory import mem_sub


@pytest.fixture
async def store(tmp_path):
    backend = SQLiteStorageBackend(f"sqlite:///{tmp_path / 'gates.db'}")
    await backend.open()
    yield backend.scheduled_loops()
    await backend.close()


def _audit(verdict: str = "proved") -> list[dict[str, str]]:
    return [
        {
            "requirement": "the deploy is green",
            "evidence": "ci status is success",
            "verdict": verdict,
        }
    ]


def test_audit_rejects_every_non_proved_verdict() -> None:
    for verdict in ("contradicted", "incomplete", "too_weak", "missing"):
        parsed = parse_audit(_audit(verdict))
        assert isinstance(parsed, list)
        blocked = audit_blocks_completion(parsed)
        assert blocked is not None
        assert verdict in blocked
        assert "stays active" in blocked
    proved = parse_audit(_audit())
    assert isinstance(proved, list)
    assert audit_blocks_completion(proved) is None
    assert "non-empty audit" in str(parse_audit([]))


def test_completion_judge_parser_fails_closed_on_bad_json() -> None:
    assert _parse_completion_judge("not json") is None
    parsed = _parse_completion_judge(
        '{"verdict":"fail","uncovered_requirements":["deploy is green"],"items":[]}'
    )
    assert parsed is not None
    assert parsed.passed is False
    assert any("deploy is green" in reason for reason in parsed.reasons)
    passed = _parse_completion_judge('{"verdict":"pass","uncovered_requirements":[],"items":[]}')
    assert passed is not None and passed.passed is True


class _ScriptedProvider:
    def __init__(self, text: str, *, delay_s: float = 0.0) -> None:
        self._text = text
        self._delay_s = delay_s

    async def stream(self, messages: object, tools: object, *, model: str) -> object:
        del messages, tools, model
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        yield TextDelta(text=self._text)
        yield Done()


@pytest.mark.asyncio
async def test_provider_completion_judge_fails_closed() -> None:
    judge = ProviderCompletionJudge(_ScriptedProvider("nope"), model="test-model", timeout_s=2)
    bad = await judge.verify("Ship it", [AuditItem("Ship it", "file exists", "proved")], "")
    assert bad.passed is False
    assert "unusable" in bad.reasons[0]
    missing = ProviderCompletionJudge(None, model="")
    unavailable = await missing.verify("Ship it", (), "")
    assert unavailable.passed is False
    slow = ProviderCompletionJudge(
        _ScriptedProvider('{"verdict":"pass"}', delay_s=0.2),
        model="test-model",
        timeout_s=0.01,
    )
    timed = await slow.verify("Ship it", (), "")
    assert timed.passed is False
    assert "timed out" in timed.reasons[0]


@pytest.mark.asyncio
async def test_update_goal_complete_requires_a_proved_audit(tmp_path, store) -> None:
    service = DurableGoalService(store)
    row, _ = await service.create(objective="Ship it", session_id="t")
    (tmp_path / "skills").mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=NoMCP(),
        scheduled_loop_store=store,
        completion_verifier=ScriptedCompletionVerifier(["pass"]),
    )
    ctx = replace(_ctx(), invoked_skill=SkillRef(name="goal", description="g"))
    missing = await ex.execute(
        call=ToolCall(
            name="update_goal",
            args={"goal_id": row.loop_id, "status": "complete"},
            call_id="1",
        ),
        ctx=ctx,
    )
    assert missing.error is not None
    assert "audit" in missing.error
    weak = await ex.execute(
        call=ToolCall(
            name="update_goal",
            args={"goal_id": row.loop_id, "status": "complete", "audit": _audit("missing")},
            call_id="2",
        ),
        ctx=ctx,
    )
    assert weak.error is not None
    assert "missing" in weak.error
    still = await store.get(row.loop_id)
    assert still is not None and still.status == "active"
    done = await ex.execute(
        call=ToolCall(
            name="update_goal",
            args={"goal_id": row.loop_id, "status": "complete", "audit": _audit()},
            call_id="3",
        ),
        ctx=ctx,
    )
    assert done.error is None
    body = json.loads(done.blocks[0].text)  # type: ignore[index]
    assert body["goal"]["status"] == "completed"
    assert body["goal"]["completion_evidence"]["judge"]["passed"] is True


@pytest.mark.asyncio
async def test_scripted_judge_fail_timeout_and_bad_json_keep_the_goal_active(
    tmp_path, store
) -> None:
    service = DurableGoalService(store)
    row, _ = await service.create(objective="Ship it", session_id="t")
    (tmp_path / "skills").mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=NoMCP(),
        scheduled_loop_store=store,
        completion_verifier=ScriptedCompletionVerifier(["fail", "timeout", "bad_json"]),
    )
    ctx = replace(_ctx(), invoked_skill=SkillRef(name="goal", description="g"))
    for call_id, needle in (("1", "not covered"), ("2", "timed out"), ("3", "unusable")):
        result = await ex.execute(
            call=ToolCall(
                name="update_goal",
                args={"goal_id": row.loop_id, "status": "complete", "audit": _audit()},
                call_id=call_id,
            ),
            ctx=ctx,
        )
        assert result.error is not None
        assert needle in result.error
    current = await store.get(row.loop_id)
    assert current is not None and current.status == "active"


@pytest.mark.asyncio
async def test_budget_pauses_and_resume_starts_a_new_window(store) -> None:
    row = await store.create(
        ScheduledLoopCreate(
            prompt="Ship it",
            interval_ms=60_000,
            session_id="sess-1",
            loop_id="goal-budget",
            max_ticks=2,
            kind=KIND_GOAL,
            objective="Ship it",
        )
    )
    for worker in ("w1", "w2"):
        await store._conn.execute(
            "UPDATE scheduled_loops SET next_tick_at_ms = 1 WHERE loop_id = ?",
            (row.loop_id,),
        )
        await store._conn.commit()
        assert await store.claim_tick(row.loop_id, worker, now_ms=1) is not None
        await store.complete_tick(row.loop_id, worker_id=worker, now_ms=1)
    paused = await store.get(row.loop_id)
    assert paused is not None
    assert paused.status == "paused"
    assert paused.stop_reason == "budget_exhausted"
    assert paused.status != "completed"
    assert await store.resume(row.loop_id) is True
    resumed = await store.get(row.loop_id)
    assert resumed is not None
    assert resumed.status == "active"
    assert resumed.budget_tick_start == paused.tick_index
    assert resumed.stop_reason is None
    await store._conn.execute(
        "UPDATE scheduled_loops SET next_tick_at_ms = 1 WHERE loop_id = ?",
        (row.loop_id,),
    )
    await store._conn.commit()
    assert await store.claim_tick(row.loop_id, "w3", now_ms=2) is not None
    continued = await store.complete_tick(row.loop_id, worker_id="w3", now_ms=2)
    assert continued is not None
    assert continued.status == "active"


@pytest.mark.asyncio
async def test_wake_active_waiting_and_paused_and_mid_tick(store) -> None:
    service = DurableGoalService(store)
    active, _ = await service.create(objective="Ship it", session_id="sess-1")
    woken = await service.wake(active.loop_id, reason="ci green", source="ci", now_ms=50)
    assert woken.status == "active"
    assert woken.next_tick_at_ms == 50
    assert woken.wake_reason == "ci green"
    await service.pause(active.loop_id)
    with pytest.raises(GoalPausedError):
        await service.wake(active.loop_id, reason="later", source="ci")
    await service.resume(active.loop_id)
    waiting = await service.update(
        goal_id=active.loop_id,
        session_id="sess-1",
        status="waiting",
        reason="needs approval",
        wake_at_ms=9_000,
    )
    assert waiting.status == "waiting"
    assert waiting.wait_reason == "needs approval"
    released = await store.release_due_waits(9_000)
    assert released == 1
    again = await store.get(active.loop_id)
    assert again is not None and again.status == "active"
    assert again.wake_reason == "recheck timer"
    await store._conn.execute(
        "UPDATE scheduled_loops SET next_tick_at_ms = 1 WHERE loop_id = ?",
        (active.loop_id,),
    )
    await store._conn.commit()
    claimed = await store.claim_tick(active.loop_id, "worker", now_ms=10)
    assert claimed is not None
    assert await store.wake(active.loop_id, reason="merged", source="github", now_ms=11)
    finished = await store.complete_tick(active.loop_id, worker_id="worker", now_ms=12)
    assert finished is not None
    assert finished.status == "active"
    assert finished.next_tick_at_ms == 12
    assert finished.wake_reason == "merged"
    prompt = format_tick_prompt(finished)
    assert "merged" in prompt
    assert "untrusted data" in prompt
    await service.stop(active.loop_id)
    with pytest.raises(GoalClosedError):
        await service.wake(active.loop_id, reason="too late", source="ci")


@pytest.mark.asyncio
async def test_waiting_goal_blocks_a_second_create(store) -> None:
    first = await store.create(
        ScheduledLoopCreate(
            prompt="First",
            interval_ms=60_000,
            session_id="sess-1",
            loop_id="goal-wait",
            kind=KIND_GOAL,
            objective="First",
        )
    )
    assert await store.set_waiting(first.loop_id, reason="review", wake_at_ms=None)
    with pytest.raises(OpenGoalExistsError):
        await store.create(
            ScheduledLoopCreate(
                prompt="Second",
                interval_ms=60_000,
                session_id="sess-1",
                loop_id="goal-other",
                kind=KIND_GOAL,
                objective="Second",
            )
        )
