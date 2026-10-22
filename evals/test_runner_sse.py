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
from runner import _path_args, run_scenario_live  # noqa: E402


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
            return httpx.Response(
                200, content=body_bytes, headers={"content-type": "text/event-stream"}
            )
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


def _run_with_events(events_for: "callable") -> list:
    import runner as runner_mod

    orig_client_cls = httpx.AsyncClient
    request_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sessions" and request.method == "POST":
            return httpx.Response(200, json={"session_id": "sess-1"})
        if path.endswith("/reply"):
            request_ids.append(json.loads(request.content)["request_id"])
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/events"):
            events = events_for(request_ids[-1] if request_ids else "")
            return httpx.Response(
                200,
                content=b"".join(_sse_block(e) for e in events),
                headers={"content-type": "text/event-stream"},
            )
        raise AssertionError(path)

    def patched_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return orig_client_cls(*args, **kwargs)

    runner_mod.httpx.AsyncClient = patched_client  # type: ignore[attr-defined]
    try:
        return asyncio.run(run_scenario_live(Scenario(id="t", messages=["go"]), "http://fake"))
    finally:
        runner_mod.httpx.AsyncClient = orig_client_cls  # type: ignore[attr-defined]


def test_tool_call_ok_parsed_from_result_payload() -> None:
    def events(rid: str) -> list[dict]:
        def started(call_id: str, tool: str, args: dict) -> dict:
            return {
                "type": "ToolCallStarted",
                "request_id": rid,
                "tool": tool,
                "call_id": call_id,
                "args": args,
            }

        def result(call_id: str, tool: str, payload: object, error: str | None = None) -> dict:
            return {
                "type": "ToolCallResult",
                "request_id": rid,
                "tool": tool,
                "call_id": call_id,
                "result": payload if isinstance(payload, str) else json.dumps(payload),
                "error": error,
            }

        return [
            started("c1", "run_command", {"argv": ["pytest"]}),
            result("c1", "run_command", {"ok": False, "exit_code": 1, "stdout": "1 failed"}),
            started("c2", "run_command", {"argv": ["pytest"]}),
            result("c2", "run_command", {"ok": True, "exit_code": 0, "stdout": "5 passed"}),
            started("c3", "read_file", {"path": "a.py"}),
            result("c3", "read_file", "plain text, not json"),
            started("c4", "replace_in_file", {"path": "a.py"}),
            result("c4", "replace_in_file", "", error="boom"),
            {"type": "TurnComplete", "request_id": rid, "usage": {}},
        ]

    calls = _run_with_events(events)[0].tool_calls
    assert [(c.tool, c.ok) for c in calls] == [
        ("run_command", False),
        ("run_command", True),
        ("read_file", True),
        ("replace_in_file", False),
    ]
    assert calls[0].error is None  # ok:false is a result, not a tool error
    assert calls[3].error == "boom"
    assert calls[1].call_id == "c2"


def test_tool_result_without_call_id_pairs_with_open_call() -> None:
    def events(rid: str) -> list[dict]:
        return [
            {"type": "ToolCallStarted", "request_id": rid, "tool": "run_command", "args": {}},
            {
                "type": "ToolCallResult",
                "request_id": rid,
                "tool": "run_command",
                "result": '{"ok": false}',
            },
            {"type": "ToolCallStarted", "request_id": rid, "tool": "run_command", "args": {}},
            {
                "type": "ToolCallResult",
                "request_id": rid,
                "tool": "run_command",
                "result": '{"ok": true}',
            },
            {"type": "TurnComplete", "request_id": rid, "usage": {}},
        ]

    calls = _run_with_events(events)[0].tool_calls
    assert [c.ok for c in calls] == [False, True]


def test_ask_user_prompts_are_answered_so_the_turn_cannot_hang() -> None:
    import runner as runner_mod

    answers: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sessions" and request.method == "POST":
            return httpx.Response(200, json={"session_id": "s1"})
        if path.endswith("/reply"):
            return httpx.Response(200, json={"ok": True})
        if "/ask-user/" in path:
            answers.append((path, json.loads(request.content)))
            return httpx.Response(202, json={"ok": True})
        if path.endswith("/events"):
            events = [
                {"type": "AskUserRequest", "tool_call_id": "call-9", "question": "mock it?"},
                {"type": "AssistantDelta", "delta": "ok"},
                {"type": "TurnComplete", "usage": {}},
            ]
            return httpx.Response(
                200,
                content=b"".join(_sse_block(e) for e in events),
                headers={"content-type": "text/event-stream"},
            )
        raise AssertionError(path)

    orig = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return orig(*args, **kwargs)

    runner_mod.httpx.AsyncClient = patched  # type: ignore[attr-defined]
    try:
        turns = asyncio.run(run_scenario_live(Scenario(id="a", messages=["go"]), "http://fake"))
    finally:
        runner_mod.httpx.AsyncClient = orig  # type: ignore[attr-defined]
    assert turns[0].output == "ok"
    assert answers == [("/sessions/s1/ask-user/call-9", {"answer": runner_mod.NO_HUMAN_ANSWER})]


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
        scenario = Scenario(
            id="cross", messages=[], sessions=[["first"], ["second"]], session_pause_sec=0
        )
        turns = asyncio.run(run_scenario_live(scenario, "http://fake-agent"))
    finally:
        runner_mod.httpx.AsyncClient = orig_client_cls  # type: ignore[attr-defined]

    assert len(turns) == 2
    assert session_post_count["n"] == 2


def test_path_args_matches_core_tool_aliases() -> None:
    assert _path_args({"file": "db/migrations/001.sql"}) == ["db/migrations/001.sql"]
    assert _path_args({"root": "db/migrations"}) == ["db/migrations"]
    assert _path_args({"path": "src/app.py", "file_path": "src/app.py"}) == [
        "src/app.py",
        "src/app.py",
    ]
    assert _path_args({"paths": ["a.py", "b.py"]}) == ["a.py", "b.py"]


def test_path_args_extracts_apply_patch_headers() -> None:
    patch = (
        "*** Begin Patch\n"
        "*** Update File: db/migrations/001.sql\n"
        "*** Move to: db/migrations/001_init.sql\n"
        "@@\n"
        "-old\n"
        "+new\n"
        "*** Add File: db/migrations/002.sql\n"
        "+create\n"
        "*** Delete File: tmp/scratch.sql\n"
        "*** End Patch\n"
    )
    paths = _path_args({"patch_text": patch})
    assert paths == [
        "db/migrations/001.sql",
        "db/migrations/001_init.sql",
        "db/migrations/002.sql",
        "tmp/scratch.sql",
    ]
    assert _path_args({"patch": "*** Add File: secret.env\n+x\n"}) == ["secret.env"]
    assert _path_args({"text": "*** Add File: ignored.sql\n+x\n"}) == []


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"\n{len(fns)} checks passed")
