"""T3-GW-01: a T3a trajectory cassette, replayed through the gateway, equals a golden stream.

The cassette is the provider side of one recorded turn: a tool call, then the final
text. The gateway runs for real around it (session, bus, SSE framing, the real
``todo_list`` tool), so the committed bytes in ``regression/t3/sse/cassette_turn.sse``
change only when the harness changes what it puts on the wire.

``--update-golden`` rewrites the golden (refused when ``CI`` is set). The cassette is built
here, so the golden has one source: this file.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, NoReturn

import pytest

from regression.drivers.replay import run_scenario
from regression.schema.assertions import event_field, event_type
from regression.schema.scenario import Scenario
from regression.schema.sse_norm import update_golden
from regression.t3.tools.cassette import CassetteDigestWarning, CassetteStaleError

_GOLDEN = "regression/t3/sse/cassette_turn.sse"
_MESSAGE = "Track a todo to write the release notes"
_REPLY = "Added the todo."


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replay that tries to open a connection fails loudly."""

    def refuse(self: socket.socket, address: object) -> NoReturn:
        raise AssertionError(f"replay tried to connect to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def _cassette_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": 1,
        "kind": "trajectory",
        "scenario": "gateway/todo_turn",
        "calls": [
            {
                "index": 0,
                "request": {"tool_names": ["todo_list"], "message_count": 2},
                "events": [
                    {"type": "thinking", "text": "A todo fits this.", "signature": "sig-1"},
                    {
                        "type": "tool_call",
                        "call_id": "c1",
                        "name": "todo_list",
                        "args": {"action": "add", "text": "Write release notes"},
                    },
                    {"type": "usage", "input_tokens": 120, "output_tokens": 18},
                    {"type": "done"},
                ],
            },
            {
                "index": 1,
                "request": {"tool_names": ["todo_list"], "message_count": 4},
                "events": [
                    {"type": "text", "text": _REPLY},
                    {"type": "usage", "input_tokens": 160, "output_tokens": 6},
                    {"type": "done"},
                ],
            },
        ],
    }
    body.update(changes)
    return body


def _scenario(tmp_path: Path, body: dict[str, Any], **assertions: Any) -> Scenario:
    cassette = tmp_path / "todo_turn.cassette.json"
    cassette.write_text(json.dumps(body), encoding="utf-8")
    checks: dict[str, Any] = {
        "turn_completed": True,
        "no_errors": True,
        "required_tools": ["todo_list"],
        "response_contains": _REPLY,
    }
    for key, value in assertions.items():
        if value is None:
            checks.pop(key, None)
        else:
            checks[key] = value
    return Scenario.model_validate(
        {
            "id": "regression/t3/test_golden_sse",
            "case": "T3-GW-01",
            "mode": "replay",
            "driver": "gateway",
            "messages": [_MESSAGE],
            "cassette": str(cassette),
            "assertions": checks,
        }
    )


@pytest.mark.case("T3-GW-01")
async def test_a_cassette_through_the_gateway_produces_the_golden_stream(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    scenario = _scenario(tmp_path, _cassette_body(), sse={"golden": _GOLDEN})
    token = update_golden.set(bool(request.config.getoption("update_golden")))
    try:
        run = await run_scenario(scenario)
    finally:
        update_golden.reset(token)

    kinds = [event_type(event) for event in run.events]
    assert kinds[-1] == "TurnComplete"
    assert "ToolCallResult" in kinds
    assert run.view.provider_calls == 2
    assert run.view.script_remaining == 0
    deltas = "".join(
        str(event_field(event, "delta"))
        for event in run.events
        if event_type(event) == "AssistantDelta"
    )
    assert deltas == _REPLY


@pytest.mark.case("T3-GW-01")
async def test_a_changed_golden_is_reported_as_a_mismatch(tmp_path: Path) -> None:
    body = _cassette_body()
    body["calls"][1]["events"][0]["text"] = "Added the todo, with a different tail."
    scenario = _scenario(tmp_path, body, sse={"golden": _GOLDEN}, response_contains=None)
    with pytest.raises(AssertionError, match=r"sse: golden mismatch for regression/t3/sse"):
        await run_scenario(scenario)


@pytest.mark.case("T3-GW-01")
async def test_a_renamed_tool_argument_is_stale_through_the_gateway(tmp_path: Path) -> None:
    body = _cassette_body()
    body["calls"][0]["events"][1]["args"] = {"action": "add", "title": "Write release notes"}
    with pytest.raises(CassetteStaleError) as caught:
        await run_scenario(_scenario(tmp_path, body))
    assert str(caught.value) == "cassette_stale: tool todo_list changed"


@pytest.mark.case("T3-GW-01")
async def test_a_changed_digest_only_warns_through_the_gateway(tmp_path: Path) -> None:
    body = _cassette_body(prompt_digest="0" * 64)
    with pytest.warns(CassetteDigestWarning, match="prompt_digest differs"):
        run = await run_scenario(_scenario(tmp_path, body, sse={"golden": _GOLDEN}))
    assert run.view.provider_calls == 2
