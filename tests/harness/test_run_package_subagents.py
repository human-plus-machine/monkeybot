"""SubagentInvocationHooks + recursion budget integration."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.core.harness.errors import RecursionBudgetExceeded
from src.core.harness.events import Principal, VersionTriple
from src.core.harness.middleware.subagent_recursion import SubagentRecursionMW
from src.core.harness.run_package_accumulator import RunPackageAccumulator, SubagentInvocationHooks


@pytest.mark.asyncio
async def test_second_nested_spawn_exceeds_recursion_budget() -> None:
    acc = RunPackageAccumulator()
    now = datetime.now(UTC)
    p = Principal()
    v = VersionTriple(harness="1", deep_agents="0.1", model="m")
    acc.begin_root("run_root", "sess", p, v, now, [])
    hooks = SubagentInvocationHooks(acc, None, SubagentRecursionMW(depth_limit=1), versions=v)
    hooks.start_subagent(
        subagent_type="outer",
        description="d1",
        session_id="sess",
        principal=p,
        parent_run_id="run_root",
        parent_tool_call_id="tc1",
    )
    with pytest.raises(RecursionBudgetExceeded):
        hooks.start_subagent(
            subagent_type="inner",
            description="d2",
            session_id="sess",
            principal=p,
            parent_run_id="run_root",
            parent_tool_call_id="tc2",
        )
    hooks.finish_success({"messages": [{"role": "assistant", "content": "done"}]}, None)
    pkg = acc.complete_root([], "pass", datetime.now(UTC))
    assert len(pkg.subagent_runs) == 1
    outer = pkg.subagent_runs[0]
    assert outer.outcome == "pass"
    assert len(outer.subagent_runs) == 1
    assert outer.subagent_runs[0].outcome == "fail"
