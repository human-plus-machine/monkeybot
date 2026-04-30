"""Unit tests for RunPackageAccumulator + subagent recursion stack."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.core.harness.events import Principal, VersionTriple
from src.core.harness.middleware.subagent_recursion import SubagentRecursionMW
from src.core.harness.run_package_accumulator import (
    RunPackageAccumulator,
    SubagentInvocationHooks,
    enter_subagent_recursion_depth,
    event_parent_run_id_for_spawn,
    exit_subagent_recursion_depth,
)


def _versions() -> VersionTriple:
    return VersionTriple(harness="1", deep_agents="0.1", model="m")


@pytest.mark.asyncio
async def test_nested_child_populates_subagent_runs() -> None:
    acc = RunPackageAccumulator()
    now = datetime.now(UTC)
    p = Principal(kind="user", id="u1")
    acc.begin_root("run_root", "sess", p, _versions(), now, [{"role": "user", "content": "root"}])
    acc.push_child(
        child_run_id="run_c1",
        session_id="sess",
        principal=p,
        versions=_versions(),
        started_at=now,
        task_description="do inner",
        subagent_type="worker",
        parent_run_id="run_root",
        parent_tool_call_id="tc1",
    )
    acc.push_child(
        child_run_id="run_c2",
        session_id="sess",
        principal=p,
        versions=_versions(),
        started_at=now,
        task_description="nested",
        subagent_type="worker",
        parent_run_id="run_c1",
        parent_tool_call_id="tc2",
    )
    ended = datetime.now(UTC)
    acc.pop_child_to_run_package([{"role": "assistant", "content": "leaf"}], "pass", ended)
    acc.pop_child_to_run_package([{"role": "assistant", "content": "mid"}], "pass", ended)
    root = acc.complete_root([{"role": "assistant", "content": "top"}], "pass", ended)
    assert len(root.subagent_runs) == 1
    mid = root.subagent_runs[0]
    assert mid.run_id == "run_c1"
    assert len(mid.subagent_runs) == 1
    assert mid.subagent_runs[0].run_id == "run_c2"


def test_event_parent_run_id_for_spawn_uses_current_child_frame() -> None:
    acc = RunPackageAccumulator()
    now = datetime.now(UTC)
    p = Principal()
    acc.begin_root("run_root", "sess", p, _versions(), now, [])
    hooks = SubagentInvocationHooks(acc, None, SubagentRecursionMW(depth_limit=3), versions=_versions())
    assert event_parent_run_id_for_spawn(hooks, "run_root") == "run_root"
    hooks.start_subagent(
        subagent_type="a",
        description="d",
        session_id="sess",
        principal=p,
        parent_run_id="run_root",
        parent_tool_call_id=None,
    )
    assert event_parent_run_id_for_spawn(hooks, "run_root") == acc.current_frame().run_id
    hooks.finish_success({"messages": [{"role": "assistant", "content": "x"}]}, None)
    acc.complete_root([{"role": "assistant", "content": "r"}], "pass", datetime.now(UTC))


def test_enter_exit_recursion_depth_stack() -> None:
    mw = SubagentRecursionMW(depth_limit=5)
    enter_subagent_recursion_depth(mw)
    enter_subagent_recursion_depth(mw)
    exit_subagent_recursion_depth()
    exit_subagent_recursion_depth()
