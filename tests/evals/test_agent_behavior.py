"""T2 — behavioral loop scenarios (YAML-driven)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.evals.scenario_runner import load_scenario, run_scenario, tool_category

SCENARIO_DIR = Path(__file__).parent / "scenarios"

SCENARIO_FILES = [
    "turn_completion.yaml",
    "tool_read.yaml",
    "tool_write.yaml",
    "tool_run_command.yaml",
    "tool_search_memory.yaml",
    "memory_write_inject.yaml",
    "subagent_roundtrip.yaml",
    "context_curation.yaml",
]


@pytest.mark.parametrize("scenario_file", SCENARIO_FILES)
@pytest.mark.asyncio
async def test_scenario(scenario_file: str) -> None:
    path = SCENARIO_DIR / scenario_file
    scenario = load_scenario(path)
    record = await run_scenario(scenario)
    a = scenario.assertions

    if a.turn_completed is True:
        assert record.completed
    if a.no_errors is True:
        assert record.errors == []
    if a.tool_categories_used:
        cats = {tool_category(name) for name in record.tool_calls}
        assert cats.intersection(set(a.tool_categories_used))
    if a.memory_injected is True:
        assert len(record.memory_injected_lines) > 0
