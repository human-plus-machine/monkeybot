"""TurnComplete optional trace_id (Story 4)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from monkeybot.core.llm.provider import Done, TextDelta
from monkeybot.core.runtime.events import TurnComplete, event_from_json, event_to_json
from monkeybot.core.runtime.loop import run
from tests.core.test_loop import AllowInspector, FakeHistory, FakeProvider, RecordingExecutor, _ctx

_TRACE_ID_HEX = re.compile(r"^[0-9a-f]{32}$")


def test_turn_complete_json_includes_trace_id_when_set() -> None:
    evt = TurnComplete(request_id="r1", trace_id="abc123")
    data = json.loads(event_to_json(evt))
    assert data["trace_id"] == "abc123"


def test_turn_complete_json_omits_trace_id_when_none() -> None:
    evt = TurnComplete(request_id="r1")
    data = json.loads(event_to_json(evt))
    assert "trace_id" not in data


def test_event_from_json_round_trip_with_trace_id() -> None:
    line = event_to_json(TurnComplete(request_id="r1", trace_id="deadbeef" * 4))
    restored = event_from_json(line)
    assert isinstance(restored, TurnComplete)
    assert restored.trace_id == "deadbeef" * 4


@pytest.mark.asyncio
async def test_loop_yields_turn_complete_with_trace_id_when_otel_on(
    otel_memory_exporter: Any,
) -> None:
    prov = FakeProvider([[TextDelta(text="hi"), Done()]])
    last: TurnComplete | None = None
    async for evt in run(
        "hello",
        _ctx(),
        provider=prov,
        history=FakeHistory(),
        inspectors=[AllowInspector()],
        tool_executor=RecordingExecutor(),
        max_turns=2,
    ):
        if isinstance(evt, TurnComplete):
            last = evt
    assert last is not None
    assert last.trace_id is not None
    assert _TRACE_ID_HEX.match(last.trace_id)
