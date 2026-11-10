"""T2-MEM-10: a drawer already in the wake-up block is not repeated by PRE_TURN recall."""

from __future__ import annotations

import pytest

from regression.drivers.loop import run_scenario
from regression.schema.events import DoneEvent, TextEvent
from regression.schema.scenario import Scenario

_FACT = "turn-one-persisted-fact"
_RECALL_HEADING = "Workspace conversation recall"


def _scenario() -> Scenario:
    return Scenario(
        id="regression/t2/python/recall_dedupe",
        case="T2-MEM-10",
        description=(
            "Turn 2's system text carries the turn-1 fact once, from wake-up, "
            "and has no recall heading."
        ),
        mode="scripted",
        hooks=["memory"],
        messages=[f"remember {_FACT}", "What did the previous turn say?"],
        script=[
            [TextEvent(type="text", text="Noted the fact."), DoneEvent(type="done")],
            [
                TextEvent(type="text", text="The previous turn mentioned it."),
                DoneEvent(type="done"),
            ],
        ],
        assertions={"turn_completed": True, "no_errors": True, "provider_calls": 2},
    )


@pytest.mark.case("T2-MEM-10")
async def test_turn_two_system_shows_the_fact_once() -> None:
    run = await run_scenario(_scenario())
    system = run.view.provider_requests[1].system
    assert system.count(_FACT) == 1
    assert _RECALL_HEADING not in system
