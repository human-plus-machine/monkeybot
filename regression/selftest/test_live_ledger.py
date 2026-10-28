"""Self-checks for the live runner's failure-ledger records and the T4-GW-01 invariant.

The gateway is a mocked transport and the model is never called. The run JSON the invariant
is checked against is regression/selftest/recorded/t4_run_smoke_sample.json.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from regression.t4 import report
from regression.t4.assertions import live_invariant_failures
from regression.t4.models import Scenario, ToolCallRecord, TurnResult, UsageSummary
from regression.t4.persistence import RunRecord, load_run
from regression.t4.runner import _collect_turn_output
from testkit.failure_ledger import LedgerWriter

RECORDED = Path(__file__).parent / "recorded" / "t4_run_smoke_sample.json"


def _sse(*events: dict[str, Any]) -> bytes:
    return b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)


def _turn_complete() -> dict[str, Any]:
    return {"type": "TurnComplete", "trace_id": "t", "usage": {"input_tokens": 1}}


def _client(body: bytes) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _collect(body: bytes) -> TurnResult:
    async with _client(body) as client:
        result, error = await _collect_turn_output(
            client, "http://agent.test", "sess", "req", "hi", duplicate_grace_sec=0.05
        )
    assert error is None
    return result


def test_the_recorded_run_has_one_turn_complete_per_turn() -> None:
    record = load_run(RECORDED)
    counts = [t.turn_completes for s in record.scenarios for t in s.turns]
    assert counts == [1, 1, 1]
    for scenario in record.scenarios:
        assert live_invariant_failures([t.turn_completes for t in scenario.turns]) == []


def test_a_duplicated_or_missing_turn_complete_in_a_recorded_run_is_a_failure() -> None:
    record = load_run(RECORDED)
    turns = record.scenarios[1].turns
    turns[1] = turns[1].model_copy(update={"turn_completes": 2})
    turns[0] = turns[0].model_copy(update={"turn_completes": 0})
    failures = live_invariant_failures([t.turn_completes for t in turns])
    assert failures == [
        "one_turn_complete_per_request (T4-GW-01): turn 1 saw 0 TurnComplete events",
        "one_turn_complete_per_request (T4-GW-01): turn 2 saw 2 TurnComplete events",
    ]


def test_a_run_file_from_before_the_runner_counted_is_not_checked() -> None:
    data = json.loads(RECORDED.read_text(encoding="utf-8"))
    for scenario in data["scenarios"]:
        for turn in scenario["turns"]:
            del turn["turn_completes"]
    record = RunRecord.model_validate(data)
    assert [t.turn_completes for s in record.scenarios for t in s.turns] == [None, None, None]
    assert live_invariant_failures([t.turn_completes for t in record.scenarios[0].turns]) == []


def test_the_runner_counts_one_turn_complete() -> None:
    result = asyncio.run(
        _collect(_sse({"type": "AssistantDelta", "delta": "hi"}, _turn_complete()))
    )
    assert result.output == "hi"
    assert result.turn_completes == 1


def test_the_runner_counts_a_duplicate_turn_complete() -> None:
    same_chunk = asyncio.run(_collect(_sse(_turn_complete(), _turn_complete())))
    assert same_chunk.turn_completes == 2


class _Chunks(httpx.AsyncByteStream):
    """A body delivered in separate chunks, like a live SSE stream."""

    def __init__(self, parts: list[bytes]) -> None:
        self._parts = parts

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self._parts:
            yield part
            await asyncio.sleep(0.01)


def test_the_runner_counts_a_duplicate_that_arrives_in_a_later_chunk() -> None:
    parts = [_sse(_turn_complete()), _sse(_turn_complete())]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, stream=_Chunks(parts))

    async def run() -> TurnResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result, error = await _collect_turn_output(
                client, "http://agent.test", "sess", "req", "hi", duplicate_grace_sec=0.5
            )
        assert error is None
        return result

    assert asyncio.run(run()).turn_completes == 2


def test_late_events_after_turn_complete_do_not_change_the_turn() -> None:
    result = asyncio.run(
        _collect(
            _sse(
                {"type": "AssistantDelta", "delta": "done"},
                _turn_complete(),
                {"type": "AssistantDelta", "delta": " extra"},
            )
        )
    )
    assert result.output == "done"
    assert result.turn_completes == 1


def _scenario(sid: str, case: str | None, required_tool: str | None) -> Scenario:
    assertions: dict[str, Any] = {"metrics": []}
    if required_tool:
        assertions["required_tools"] = [required_tool]
    return Scenario(id=sid, case=case, messages=["go"], assertions=assertions)


def _turn(tool: str | None, completes: int = 1) -> TurnResult:
    calls = [ToolCallRecord(tool=tool, ok=True)] if tool else []
    return TurnResult(
        input="go",
        output="done",
        usage=UsageSummary(input_tokens=10, output_tokens=5, duration_ms=1200),
        tool_calls=calls,
        turn_completes=completes,
    )


def test_each_failed_scenario_is_one_ledger_failure_with_the_run_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scenarios = [
        _scenario("file/ok", "T4-FILE-01", "read_file"),
        _scenario("file/missing_tool", "T4-FILE-02", "write_file"),
        _scenario("file/dup_complete", "T4-FILE-03", None),
        _scenario("file/crashes", None, None),
    ]
    turns = {
        "file/ok": _turn("read_file"),
        "file/missing_tool": _turn("read_file"),
        "file/dup_complete": _turn(None, completes=2),
    }

    async def fake_live(scenario: Scenario, agent_url: str, **_: Any) -> list[TurnResult]:
        assert agent_url == "http://127.0.0.1:8787"
        if scenario.id == "file/crashes":
            raise RuntimeError("SSE ended before TurnComplete")
        return [turns[scenario.id]]

    monkeypatch.setattr(report, "run_scenario_live", fake_live)
    monkeypatch.setattr(report, "load_scenarios_by_id", lambda ids: scenarios)
    monkeypatch.setattr(
        report, "_model_metadata", lambda: ("ollama-cloud", "glm-5.3-flash", "ollama-cloud", "j")
    )

    ledger = LedgerWriter(tmp_path / "ledger")
    ledger.start(argv=["python", "-m", "regression.t4"], live={"suite": "smoke"})
    runs = tmp_path / "runs"
    record = asyncio.run(
        report.run_suite(
            "smoke",
            [s.id for s in scenarios],
            "http://127.0.0.1:8787",
            ledger=ledger,
            runs_dir=runs,
        )
    )
    report._finish_ledger(ledger, record, 1)

    assert [s.status for s in record.scenarios] == ["passed", "failed", "failed", "errored"]
    failures = ledger.failures
    assert [f["case"] for f in failures] == ["T4-FILE-02", "T4-FILE-03", None]
    assert [f["outcome"] for f in failures] == ["failed", "failed", "error"]
    assert [f["tier"] for f in failures] == ["t4", "t4", "t4"]
    assert "required_tools" in failures[0]["message"]
    assert "T4-GW-01" in failures[1]["message"]
    assert "SSE ended before TurnComplete" in failures[2]["message"]

    run_json = runs / f"{record.run_id}.json"
    for failure in failures:
        assert failure["artifacts"] == [str(run_json)]
        assert failure["live"]["provider"] == "ollama-cloud"
        assert failure["live"]["model"] == "glm-5.3-flash"
        assert failure["live"]["agent_url"] == "http://127.0.0.1:8787"
        assert failure["path"].startswith("regression/t4/scenarios/")
        assert "--suite smoke" in failure["repro"]
    saved = load_run(run_json)
    assert [s.scenario_id for s in saved.scenarios] == [s.id for s in scenarios]

    lines = [json.loads(x) for x in ledger.path.read_text(encoding="utf-8").splitlines()]  # type: ignore[union-attr]
    assert [x["kind"] for x in lines] == ["run_start", "failure", "failure", "failure", "run_end"]
    assert lines[-1]["counts"] == {
        "passed": 1,
        "failed": 2,
        "errors": 1,
        "skipped": 0,
        "xfailed": 0,
        "xpassed_strict": 0,
    }


def test_passing_scenarios_write_no_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_live(scenario: Scenario, agent_url: str, **_: Any) -> list[TurnResult]:
        return [_turn("read_file")]

    monkeypatch.setattr(report, "run_scenario_live", fake_live)
    monkeypatch.setattr(
        report,
        "load_scenarios_by_id",
        lambda ids: [_scenario("file/ok", "T4-FILE-01", "read_file")],
    )
    monkeypatch.setattr(report, "_model_metadata", lambda: ("p", "m", "jp", "jm"))
    ledger = LedgerWriter(tmp_path / "ledger")
    ledger.start(argv=["x"])
    record = asyncio.run(
        report.run_suite("smoke", ["file/ok"], "http://x", ledger=ledger, runs_dir=tmp_path / "r")
    )
    assert record.suite_aggregate.passed_count == 1
    assert ledger.failures == []
