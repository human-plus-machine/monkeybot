"""Offline checks for the live assertion keys, live steps, and gateway grouping."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

from regression.plugin import suite_keeps_python
from regression.schema.assertions import RunView, evaluate
from regression.schema.steps import (
    CreateLoopStep,
    DueSteps,
    EventPredicate,
    ReloadConfigStep,
)
from regression.t4.assertions import evaluate_assertions
from regression.t4.gateway import (
    Gateway,
    GatewayPool,
    GatewaySpec,
    group_in_order,
    spec_from_fields,
)
from regression.t4.models import EvalRun, Scenario, TurnResult
from regression.t4.scenario_loader import SCENARIOS_ROOT


def _run(turns: list[TurnResult], **extra: object) -> EvalRun:
    return EvalRun(run_id="r", scenario_id="s", turns=turns, **extra)  # type: ignore[arg-type]


def _started(tool: str, args: dict) -> dict:
    return {"type": "ToolCallStarted", "tool": tool, "args": args}


def test_stop_guard_fails_when_the_call_has_none() -> None:
    scenario = Scenario(id="s", assertions={"tool_args": {"start_loop": {"stop_guard": True}}})
    turn = TurnResult(
        input="check",
        output="",
        events=[_started("start_loop", {"prompt": "check", "interval": "5m"})],
    )
    failures = evaluate_assertions(scenario, _run([turn]))
    assert any("stop guard" in failure for failure in failures)


def test_stop_guard_passes_with_max_ticks() -> None:
    scenario = Scenario(id="s", assertions={"tool_args": {"start_loop": {"stop_guard": True}}})
    turn = TurnResult(
        input="check",
        output="",
        events=[_started("start_loop", {"prompt": "check", "interval": "5m", "max_ticks": 3})],
    )
    failures = evaluate_assertions(scenario, _run([turn]))
    assert not any(failure.startswith("tool_args") for failure in failures)


def test_per_session_fails_when_the_search_is_on_the_other_session() -> None:
    scenario = Scenario(
        id="s",
        assertions={
            "per_session": {
                2: {
                    "response_contains": "4419",
                    "tool_args": {"run_command": {"command_contains": ["mempalace", "search"]}},
                }
            }
        },
    )
    first = TurnResult(
        input="remember",
        output="4419",
        session_index=1,
        events=[
            _started("run_command", {"argv": ["mempalace", "search", "4419"]}),
            {"type": "AssistantTextEnded", "text": "4419"},
        ],
    )
    second = TurnResult(
        input="search",
        output="I do not know",
        session_index=2,
        events=[{"type": "AssistantTextEnded", "text": "I do not know"}],
    )
    failures = evaluate_assertions(scenario, _run([first, second]))
    assert any("per_session.2" in failure and "was not called" in failure for failure in failures)
    assert any("per_session.2" in failure and "4419" in failure for failure in failures)


def test_per_session_passes_on_the_session_that_searched() -> None:
    scenario = Scenario(
        id="s",
        assertions={
            "per_session": {
                2: {
                    "response_contains": "4419",
                    "tool_args": {"run_command": {"command_contains": ["mempalace", "search"]}},
                }
            }
        },
    )
    second = TurnResult(
        input="search",
        output="4419",
        session_index=2,
        events=[
            _started("run_command", {"argv": ["mempalace", "search", "vault"]}),
            {"type": "AssistantTextEnded", "text": "The code is 4419."},
        ],
    )
    failures = evaluate_assertions(scenario, _run([second]))
    assert not any(failure.startswith("per_session") for failure in failures)


def test_evidence_fails_when_the_cited_path_was_not_read(tmp_path: Path) -> None:
    cited = tmp_path / "calc" / "ops.py"
    cited.parent.mkdir()
    cited.write_text("def add():\n    return 1\n", encoding="utf-8")
    view = RunView(
        workspace=tmp_path,
        events=[
            {"type": "AssistantTextEnded", "text": "Evidence: calc/ops.py"},
        ],
    )
    failures = evaluate({"evidence_paths": True}, view)
    assert any("was not read" in failure for failure in failures)


def test_evidence_fails_when_the_cited_path_is_missing(tmp_path: Path) -> None:
    view = RunView(
        workspace=tmp_path,
        events=[
            _started("read_file", {"path": "calc/missing.py"}),
            {"type": "AssistantTextEnded", "text": "Evidence: calc/missing.py"},
        ],
    )
    failures = evaluate({"evidence_paths": True}, view)
    assert any("does not exist" in failure for failure in failures)


def test_evidence_passes_when_the_cited_file_was_read(tmp_path: Path) -> None:
    cited = tmp_path / "eval-case" / "calc" / "ops.py"
    cited.parent.mkdir(parents=True)
    cited.write_text("def add():\n    return 1\n", encoding="utf-8")
    view = RunView(
        workspace=tmp_path,
        events=[
            _started("read_file", {"path": "eval-case/calc/ops.py"}),
            {
                "type": "AssistantTextEnded",
                "text": "It defines add.\nEvidence: eval-case/calc/ops.py",
            },
        ],
    )
    assert evaluate({"evidence_paths": True}, view) == []


def test_manifest_fails_on_the_wrong_subagent_type() -> None:
    scenario = Scenario(id="s", assertions={"subagent_manifest": {"subagent_type": "researcher"}})
    failures = evaluate_assertions(
        scenario, _run([], manifests=[{"type": "SessionManifest", "subagent_type": "general"}])
    )
    assert any("researcher" in failure for failure in failures)


def test_manifest_passes_when_the_child_records_the_persona() -> None:
    scenario = Scenario(id="s", assertions={"subagent_manifest": {"subagent_type": "researcher"}})
    failures = evaluate_assertions(
        scenario, _run([], manifests=[{"type": "SessionManifest", "subagent_type": "researcher"}])
    )
    assert not any(failure.startswith("subagent_manifest") for failure in failures)


def test_system_prompt_fails_when_the_last_turn_still_has_the_heading() -> None:
    scenario = Scenario(
        id="s", assertions={"system_prompt": {"last_not_contains": ["## Memory wake-up"]}}
    )
    run = _run(
        [TurnResult(input="a", output="ready"), TurnResult(input="b", output="ready")],
        system_prompts=["## Memory wake-up\nstuff", "still here\n## Memory wake-up"],
    )
    failures = evaluate_assertions(scenario, run)
    assert any("Memory wake-up" in failure for failure in failures)


def test_system_prompt_fails_when_nothing_was_recorded() -> None:
    scenario = Scenario(
        id="s", assertions={"system_prompt": {"last_not_contains": ["## Memory wake-up"]}}
    )
    failures = evaluate_assertions(scenario, _run([TurnResult(input="a", output="ready")]))
    assert any("no system prompt" in failure for failure in failures)


def test_system_prompt_passes_when_the_last_turn_has_no_heading() -> None:
    scenario = Scenario(
        id="s", assertions={"system_prompt": {"last_not_contains": ["## Memory wake-up"]}}
    )
    run = _run(
        [TurnResult(input="a", output="ready"), TurnResult(input="b", output="ready")],
        system_prompts=["## Memory wake-up\nstuff", "You are the agent."],
    )
    assert not any(
        failure.startswith("system_prompt") for failure in evaluate_assertions(scenario, run)
    )


def test_chat_history_fails_when_the_user_row_is_not_the_tick_prompt() -> None:
    scenario = Scenario(
        id="s",
        assertions={
            "chat_history": {
                "one_user_message": True,
                "user_equals_turn_input": True,
                "assistant_nonempty": True,
            }
        },
    )
    turn = TurnResult(input="[SCHEDULED TICK 1/1]\n\nReply with pong.", output="pong")
    run = _run(
        [turn],
        history_snapshots=[
            [{"role": "user", "text": "Reply with pong."}, {"role": "assistant", "text": "pong"}]
        ],
    )
    failures = evaluate_assertions(scenario, run)
    assert any("does not equal the tick prompt" in failure for failure in failures)


def test_chat_history_passes_when_the_row_matches_the_prompt() -> None:
    prompt = "[SCHEDULED TICK 1/1 · loop_id=abc · session=sess]\n\nReply with pong."
    scenario = Scenario(
        id="s",
        assertions={
            "chat_history": {
                "one_user_message": True,
                "user_equals_turn_input": True,
                "assistant_nonempty": True,
            }
        },
    )
    turn = TurnResult(input=prompt, output="pong")
    run = _run(
        [turn],
        history_snapshots=[
            [{"role": "user", "text": prompt}, {"role": "assistant", "text": "pong"}]
        ],
    )
    assert not any(
        failure.startswith("chat_history") for failure in evaluate_assertions(scenario, run)
    )


def test_deltas_equal_final_fails_when_a_delta_is_repeated() -> None:
    frames = [
        {"type": "AssistantDelta", "request_id": "r1", "delta": "Hello "},
        {"type": "AssistantDelta", "request_id": "r1", "delta": "Hello "},
        {"type": "AssistantTextEnded", "request_id": "r1", "text": "Hello world"},
    ]
    scenario = Scenario(id="s", assertions={"sse": {"deltas_equal_final": True}})
    turn = TurnResult(input="x", output="Hello world", frames=frames, events=frames)
    failures = evaluate_assertions(scenario, _run([turn]))
    assert any("differs from the final text" in failure for failure in failures)


def test_deltas_equal_final_passes_when_the_pieces_join() -> None:
    frames = [
        {"type": "AssistantDelta", "request_id": "r1", "delta": "Hello "},
        {"type": "AssistantDelta", "request_id": "r1", "delta": "world"},
        {"type": "AssistantTextEnded", "request_id": "r1", "text": "Hello world"},
    ]
    scenario = Scenario(id="s", assertions={"sse": {"deltas_equal_final": True}})
    turn = TurnResult(input="x", output="Hello world", frames=frames, events=frames)
    assert not any(
        failure.startswith("sse:") for failure in evaluate_assertions(scenario, _run([turn]))
    )


def test_a_setup_step_is_not_due_on_an_event() -> None:
    step = CreateLoopStep(action="create_loop", prompt="ping")
    assert DueSteps([step]).take({"type": "TurnComplete"}) == []


class _FakeLauncher:
    def __init__(self) -> None:
        self.started: list[GatewaySpec] = []
        self.stopped: list[GatewaySpec] = []

    def start(self, spec: GatewaySpec, config_path: Path, data_dir: Path) -> Gateway:
        self.started.append(spec)
        return Gateway(
            url=f"http://fake/{len(self.started)}",
            spec=spec,
            managed=True,
            config_path=config_path,
            data_dir=data_dir,
        )

    def stop(self, gateway: Gateway) -> None:
        self.stopped.append(gateway.spec)


def _pool(tmp_path: Path, launcher: _FakeLauncher) -> GatewayPool:
    config = tmp_path / "config"
    config.mkdir()
    (config / "monkeybot.yaml").write_text("model:\n  name: x\n", encoding="utf-8")
    return GatewayPool(
        external_url="http://external",
        launcher=launcher,
        config_dir=config,
        data_root=tmp_path / "data",
    )


def test_the_same_spec_starts_one_gateway_and_a_different_spec_starts_another(
    tmp_path: Path,
) -> None:
    launcher = _FakeLauncher()
    pool = _pool(tmp_path, launcher)
    default = pool.acquire(GatewaySpec())
    assert default.url == "http://external"
    assert launcher.started == []
    spec = GatewaySpec(overrides_json=json.dumps({"model": {"context_window": 6000}}))
    first = pool.acquire(spec)
    second = pool.acquire(spec)
    assert first is second
    assert len(launcher.started) == 1
    other = GatewaySpec(overrides_json=json.dumps({"model": {"context_window": 8000}}))
    third = pool.acquire(other)
    assert third is not first
    assert len(launcher.started) == 2
    pool.close()


def test_an_exclusive_gateway_stops_when_released(tmp_path: Path) -> None:
    launcher = _FakeLauncher()
    pool = _pool(tmp_path, launcher)
    spec = GatewaySpec(exclusive=True)
    gateway = pool.acquire(spec)
    pool.release(gateway)
    assert launcher.stopped == [spec]
    again = pool.acquire(spec)
    assert again is not gateway
    assert len(launcher.started) == 2
    pool.close()


def test_grouping_keeps_one_spec_together_and_exclusive_specs_apart() -> None:
    shared = GatewaySpec(overrides_json=json.dumps({"model": {"context_window": 6000}}))
    other = GatewaySpec(env=(("MCP_CONFIG", "mcp.progressive.json"),))
    exclusive = GatewaySpec(exclusive=True)
    items = [("a", shared), ("b", other), ("a2", shared), ("e1", exclusive), ("e2", exclusive)]
    grouped = group_in_order(items, lambda item: item[1])
    assert [item[0] for item in grouped] == ["a", "a2", "b", "e1", "e2"]


def test_reading_scenario_specs_does_not_start_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a process was started")

    monkeypatch.setattr(subprocess, "Popen", boom)
    specs = []
    for path in sorted(SCENARIOS_ROOT.rglob("*.yaml")):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = loaded.get("config") if isinstance(loaded.get("config"), dict) else {}
        steps = loaded.get("steps") if isinstance(loaded.get("steps"), list) else []
        assertions = loaded.get("assertions") if isinstance(loaded.get("assertions"), dict) else {}
        specs.append(
            spec_from_fields(
                config,
                verifier_blocks=bool({"verifier_on", "verifier_off"} & set(assertions)),
                reload_steps=any(
                    isinstance(step, dict) and step.get("action") == "reload_config"
                    for step in steps
                ),
                verifier_mode=None,
            )
        )
    assert group_in_order(specs, lambda spec: spec)


def test_suite_python_filter_names_files_and_an_absent_suite_keeps_them_all() -> None:
    named = frozenset({"test_provider_live_matrix.py"})
    assert suite_keeps_python("test_provider_live_matrix.py", None)
    assert suite_keeps_python("test_docker_image_health.py", None)
    assert suite_keeps_python("test_provider_live_matrix.py", named)
    assert not suite_keeps_python("test_docker_image_health.py", named)


def test_reload_config_rewrites_the_managed_file_and_posts(tmp_path: Path) -> None:
    from regression.t4.runner import _reload_config

    config = tmp_path / "agent.yaml"
    config.write_text("memory:\n  enabled: true\n", encoding="utf-8")
    gateway = Gateway(
        url="http://fake",
        spec=GatewaySpec(exclusive=True),
        managed=True,
        config_path=config,
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200, json={"revision": 2, "digest": "abc", "changed": [], "applied": []}
        )

    step = ReloadConfigStep(
        action="reload_config",
        config={"memory.enabled": False},
        when=EventPredicate(event="TurnComplete"),
    )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await _reload_config(client, "http://fake", step, gateway, httpx.Timeout(5.0))

    asyncio.run(run())
    assert seen == ["/admin/config/reload"]
    assert "enabled: false" in config.read_text(encoding="utf-8")


def test_reload_config_fails_without_a_managed_gateway() -> None:
    from regression.t4.runner import _reload_config

    step = ReloadConfigStep(
        action="reload_config",
        config={"memory.enabled": False},
        when=EventPredicate(event="TurnComplete"),
    )

    async def run() -> None:
        async with httpx.AsyncClient() as client:
            await _reload_config(client, "http://fake", step, None, httpx.Timeout(5.0))

    with pytest.raises(RuntimeError, match="gateway the runner started"):
        asyncio.run(run())


def _sse(event: dict, event_id: str | None = None) -> bytes:
    prefix = f"id: {event_id}\n" if event_id else ""
    return f"{prefix}data: {json.dumps(event)}\n\n".encode()


def test_resume_sends_the_last_event_id_and_joins_the_deltas() -> None:
    from regression.t4.runner import run_scenario_live

    headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sessions" and request.method == "POST":
            return httpx.Response(200, json={"session_id": "sess-1"})
        if path.endswith("/reply"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/events"):
            last = request.headers.get("last-event-id")
            headers.append(last)
            if last == "1":
                body = b"".join(
                    [
                        _sse({"type": "AssistantDelta", "delta": "world"}, "2"),
                        _sse({"type": "AssistantTextEnded", "text": "Hello world"}, "3"),
                        _sse(
                            {"type": "TurnComplete", "usage": {"duration_ms": 1}},
                            "4",
                        ),
                    ]
                )
            else:
                body = _sse({"type": "AssistantDelta", "delta": "Hello "}, "1")
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        raise AssertionError(path)

    from regression.schema.steps import DisconnectSseStep, ReconnectSseStep

    scenario = Scenario(
        id="gateway/live_resume",
        messages=["Write three short sentences."],
        steps=[
            DisconnectSseStep(action="disconnect_sse", when=EventPredicate(event="text")),
            ReconnectSseStep(
                action="reconnect_sse",
                last_event_id="last_seen",
                when=EventPredicate(event="text"),
            ),
        ],
    )

    from regression.t4 import runner as runner_mod

    original = runner_mod.httpx.AsyncClient

    def patched(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)  # type: ignore[index]
        return original(*args, **kwargs)  # type: ignore[operator]

    runner_mod.httpx.AsyncClient = patched  # type: ignore[misc, assignment]
    try:
        turns = asyncio.run(run_scenario_live(scenario, "http://fake"))
    finally:
        runner_mod.httpx.AsyncClient = original  # type: ignore[misc]
    assert headers == [None, "1"]
    assert turns[0].output == "Hello world"
    assert not evaluate_assertions(
        Scenario(id="s", assertions={"sse": {"deltas_equal_final": True}}),
        _run(list(turns)),
    )


def test_http_tick_posts_the_formatted_prompt_and_reads_it_back() -> None:
    from regression.t4.runner import run_scenario_live

    posted: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sessions" and request.method == "POST":
            return httpx.Response(200, json={"session_id": "sess-loop"})
        if path == "/scheduler/loops" and request.method == "POST":
            body = json.loads(request.content)
            assert body["confirmed"] is True
            return httpx.Response(
                201,
                json={
                    "loop": {
                        "loop_id": "loop-1",
                        "session_id": "sess-loop",
                        "status": "active",
                        "prompt": body["prompt"],
                        "interval_ms": 3600000,
                        "max_ticks": 1,
                        "max_runtime_ms": None,
                        "skip_if_busy": True,
                        "tick_index": 0,
                        "next_tick_at_ms": 0,
                        "started_at_ms": 0,
                        "last_tick_at_ms": None,
                        "last_error": None,
                        "stop_reason": None,
                        "tick_in_flight": False,
                        "kind": "loop",
                    }
                },
            )
        if path == "/scheduler/invoke-tick":
            posted["message"] = json.loads(request.content)["message"]
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/events"):
            body = b"".join(
                [
                    _sse({"type": "AssistantDelta", "delta": "pong"}),
                    _sse({"type": "AssistantTextEnded", "text": "pong"}),
                    _sse({"type": "TurnComplete", "usage": {"duration_ms": 1}}),
                ]
            )
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
        if path.startswith("/api/chat-history/"):
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {"role": "user", "text": posted["message"]},
                        {"role": "assistant", "text": "pong"},
                    ]
                },
            )
        if path.endswith("/stop"):
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(path)

    from regression.schema.steps import CreateLoopStep, InvokeTickStep, ReadHistoryStep

    scenario = Scenario(
        id="scheduler/http_one_tick_live",
        steps=[
            CreateLoopStep(action="create_loop", prompt="Reply with the single word pong."),
            InvokeTickStep(action="invoke_tick"),
            ReadHistoryStep(action="read_history"),
        ],
    )

    from regression.t4 import runner as runner_mod

    original = runner_mod.httpx.AsyncClient

    def patched(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)  # type: ignore[index]
        return original(*args, **kwargs)  # type: ignore[operator]

    runner_mod.httpx.AsyncClient = patched  # type: ignore[misc, assignment]
    try:
        produced = asyncio.run(run_scenario_live(scenario, "http://fake"))
    finally:
        runner_mod.httpx.AsyncClient = original  # type: ignore[misc]
    assert posted["message"].startswith("[SCHEDULED TICK 1/1")
    assert "Reply with the single word pong." in posted["message"]
    assert produced[0].input == posted["message"]
    snapshot = produced.history_snapshots[0]
    assert snapshot[0]["text"] == posted["message"]
    failures = evaluate_assertions(
        Scenario(
            id="s",
            assertions={
                "chat_history": {
                    "one_user_message": True,
                    "user_equals_turn_input": True,
                    "assistant_nonempty": True,
                }
            },
        ),
        _run(list(produced), history_snapshots=produced.history_snapshots),
    )
    assert failures == []
