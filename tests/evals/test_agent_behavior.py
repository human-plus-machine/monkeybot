"""T2 — behavioral loop scenarios (YAML-driven)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.evals.scenario_runner import check_assertions, load_scenario, run_scenario

SCENARIO_DIR = Path(__file__).parent / "scenarios"

SCENARIO_FILES = [
    "turn_completion.yaml",
    "tool_read.yaml",
    "tool_write.yaml",
    "tool_run_command.yaml",
    "tool_search_memory.yaml",
    "memory_write_inject.yaml",
    "subagent_roundtrip.yaml",
    "memory_wake_up_large.yaml",
    "tool_loops.yaml",
    "tool_glob_grep.yaml",
    "tool_replace_apply_patch.yaml",
    "tool_search_knowledge.yaml",
    "tool_load_file.yaml",
    "tool_web_search.yaml",
    "tool_todo_list.yaml",
]


@pytest.mark.parametrize("scenario_file", SCENARIO_FILES)
@pytest.mark.asyncio
async def test_scenario(scenario_file: str) -> None:
    path = SCENARIO_DIR / scenario_file
    scenario = load_scenario(path)
    record = await run_scenario(scenario)
    check_assertions(record, scenario.assertions)
