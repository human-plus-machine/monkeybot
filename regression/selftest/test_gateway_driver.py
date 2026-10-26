"""One gateway scenario, in-process, with invariants 5 and 8 enabled.

Invariant 6 (``sse_schema``) is skipped: ``tests/contracts/goldens/sse/schema.json``
belongs to P2-02 and is not in the tree. Invariant 5 is ``no_sse_intervention``.
Invariant 8 is ``turn_lock``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from monkeybot.core.testing.mocks_provider import load_scripted_provider
from regression.drivers.gateway import GatewayDriverError, run_scenario
from regression.schema.assertions import event_type
from regression.schema.scenario import load_scenario

_FIXTURE = (
    Path(__file__).resolve().parents[2] / "regression" / "fixtures" / "scripts" / "one_turn.yaml"
)
_SKIP = """\
invariants_skip:
  - name: sse_schema
    reason: tests/contracts/goldens/sse/schema.json is created by P2-02
"""


def test_one_turn_fixture_is_a_provider_script() -> None:
    provider = load_scripted_provider(_FIXTURE)
    assert provider.name == "fake"


def _load(tmp_path: Path, name: str, body: str):
    path = tmp_path / f"{name}.yaml"
    path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
    return load_scenario(path, root=tmp_path)


def _script_block() -> str:
    lines = [
        line
        for line in _FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return "\n".join(f"  {line}" for line in lines)


def _one_turn(tmp_path: Path, *, steps: str = ""):
    parts = [
        "case: P2-01",
        "description: one scripted turn through the gateway",
        "mode: scripted",
        "driver: gateway",
        "messages:",
        "  - Say hello",
        "script:",
        _script_block(),
        "assertions:",
        "  turn_completed: true",
        "  no_errors: true",
        "  response_contains: hello",
    ]
    step_block = textwrap.dedent(steps).strip("\n")
    if step_block:
        parts.append(step_block)
    parts.append(_SKIP.strip("\n"))
    return _load(tmp_path, "one_turn", "\n".join(parts))


async def test_one_turn_gateway_scenario_passes_invariants_5_and_8(tmp_path: Path) -> None:
    scenario = _one_turn(tmp_path)
    skipped = {item.name for item in scenario.invariants_skip}
    assert "no_sse_intervention" not in skipped
    assert "turn_lock" not in skipped

    result = await run_scenario(scenario)

    assert result.view.driver == "gateway"
    assert result.view.second_reply_status == 200
    assert result.view.script_remaining == 0
    assert event_type(result.events[-1]) == "TurnComplete"
    assert not any(event_type(event) == "HarnessIntervention" for event in result.view.sse_frames)


async def test_disconnect_and_reconnect_still_reach_turn_complete(tmp_path: Path) -> None:
    scenario = _one_turn(
        tmp_path,
        steps="""
        steps:
          - action: disconnect_sse
            when: {event: AssistantDelta}
          - action: reconnect_sse
            last_event_id: "1"
            when: {event: AssistantDelta}
        """,
    )
    result = await run_scenario(scenario)
    assert result.view.second_reply_status == 200
    assert "1" in result.view.sse_replay
    assert event_type(result.events[-1]) == "TurnComplete"


async def test_realtime_steps_are_rejected(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        "realtime_step",
        """
        case: P2-01-reject
        mode: scripted
        driver: gateway
        messages: [go]
        script:
          - - type: text
              text: hello
            - type: done
        steps:
          - action: client_text
            text: hello from the client
            when: {event: text}
        """,
    )
    with pytest.raises(GatewayDriverError, match="realtime"):
        await run_scenario(scenario)
