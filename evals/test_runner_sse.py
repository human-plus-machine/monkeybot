"""Assert-based self-check for evals/runner.py SSE telemetry parsing (mocked transport, no network).

Run: python evals/test_runner_sse.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from models import Scenario  # noqa: E402
from runner import run_scenario_live  # noqa: E402


def _sse_block(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


def _make_transport(request_id_holder: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sessions" and request.method == "POST":
            return httpx.Response(200, json={"session_id": "sess-1"})
        if path.endswith("/reply") and request.method == "POST":
            body = json.loads(request.content)
            request_id_holder.append(body["request_id"])
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/events") and request.method == "GET":
            rid = request_id_holder[-1] if request_id_holder else ""
            events = [
                {"type": "AssistantDelta", "request_id": rid, "delta": "Hello "},
                {
                    "type": "ToolCallStarted",
                    "request_id": rid,
                    "tool": "read_file",
                    "args": {"path": "AGENT.md"},
                },
                {"type": "ToolCallResult", "request_id": rid, "tool": "read_file", "result": "ok"},
                {"type": "ContextSummarized", "request_id": rid, "turns_summarized": 3},
                {
                    "type": "VerifierVerdict",
                    "request_id": rid,
                    "status": "on_track",
                    "severity": "none",
                    "triggering_signals": ["budget_burn"],
                },
                {"type": "AssistantDelta", "request_id": rid, "delta": "world"},
                {
                    "type": "TurnComplete",
                    "request_id": rid,
                    "trace_id": "trace-123",
                    "usage": {
                        "input_tokens": 42,
                        "output_tokens": 7,
                        "cached_tokens": 1,
                        "cost_usd": 0.002,
                        "duration_ms": 850,
                    },
                },
            ]
            body_bytes = b"".join(_sse_block(e) for e in events)
            return httpx.Response(200, content=body_bytes, headers={"content-type": "text/event-stream"})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    return httpx.MockTransport(handler)


def test_single_session_telemetry() -> None:
    import runner as runner_mod

    orig_client_cls = httpx.AsyncClient
    request_ids: list[str] = []

    def patched_client(*args, **kwargs):
        kwargs["transport"] = _make_transport(request_ids)
        return orig_client_cls(*args, **kwargs)

    runner_mod.httpx.AsyncClient = patched_client  # type: ignore[attr-defined]
    try:
        scenario = Scenario(id="t", messages=["hi there"])
        turns = asyncio.run(run_scenario_live(scenario, "http://fake-agent"))
    finally:
        runner_mod.httpx.AsyncClient = orig_client_cls  # type: ignore[attr-defined]

    assert len(turns) == 1
    t = turns[0]
    assert t.output == "Hello world"
    assert t.trace_id == "trace-123"
    assert t.usage.input_tokens == 42
    assert t.usage.output_tokens == 7
    assert t.usage.duration_ms == 850
    assert t.summarizations_count == 1
    assert len(t.tool_calls) == 1
    assert t.tool_calls[0].tool == "read_file"
    assert t.tool_calls[0].error is None
    assert t.tool_calls[0].path_args == ["AGENT.md"]
    assert len(t.verdicts) == 1
    assert t.verdicts[0].status == "on_track"
    assert t.verdicts[0].severity == "none"
    assert t.verdicts[0].triggering_signals == ["budget_burn"]


def test_cross_session_opens_two_sessions() -> None:
    import runner as runner_mod

    session_post_count = {"n": 0}
    orig_client_cls = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions" and request.method == "POST":
            session_post_count["n"] += 1
            return httpx.Response(200, json={"session_id": f"sess-{session_post_count['n']}"})
        if request.url.path.endswith("/reply"):
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith("/events"):
            rid = ""
            events = [
                {"type": "AssistantDelta", "request_id": rid, "delta": "ok"},
                {"type": "TurnComplete", "request_id": rid, "usage": {}},
            ]
            return httpx.Response(
                200,
                content=b"".join(_sse_block(e) for e in events),
                headers={"content-type": "text/event-stream"},
            )
        raise AssertionError(request.url.path)

    def patched_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return orig_client_cls(*args, **kwargs)

    runner_mod.httpx.AsyncClient = patched_client  # type: ignore[attr-defined]
    try:
        scenario = Scenario(id="cross", messages=[], sessions=[["first"], ["second"]], session_pause_sec=0)
        turns = asyncio.run(run_scenario_live(scenario, "http://fake-agent"))
    finally:
        runner_mod.httpx.AsyncClient = orig_client_cls  # type: ignore[attr-defined]

    assert len(turns) == 2
    assert session_post_count["n"] == 2


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"\n{len(fns)} checks passed")
