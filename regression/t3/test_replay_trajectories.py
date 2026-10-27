"""Replay driver checks with hand-written cassettes (no live model, no network).

The recorded trajectories under ``regression/t3/trajectories/`` run as YAML items
through the plugin. This module covers the driver itself: a cassette replays, a
renamed tool argument is ``cassette_stale``, and a digest-only difference warns
and still passes.
"""

from __future__ import annotations

import json
import socket
import warnings
from pathlib import Path
from typing import Any, NoReturn

import pytest

from regression.drivers.replay import (
    ReplayDriverError,
    resolve_cassette,
    run_scenario,
    scripted_scenario,
)
from regression.schema.assertions import event_field, event_type
from regression.schema.scenario import Scenario
from regression.t3.tools.cassette import (
    Cassette,
    CassetteDigestWarning,
    CassetteStaleError,
    load_cassette,
)


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
        "scenario": "hand/list_then_answer",
        "calls": [
            {
                "index": 0,
                "request": {"tool_names": ["list_dir"], "message_count": 2},
                "events": [
                    {"type": "thinking", "text": "look first", "signature": "sig"},
                    {
                        "type": "tool_call",
                        "call_id": "c1",
                        "name": "list_dir",
                        "args": {"path": "."},
                    },
                    {"type": "text", "text": "Looking."},
                    {"type": "usage", "input_tokens": 10, "output_tokens": 3},
                    {"type": "done"},
                ],
            },
            {
                "index": 1,
                "events": [
                    {"type": "text", "text": "Found it."},
                    {"type": "usage", "input_tokens": 12, "output_tokens": 2},
                    {"type": "done"},
                ],
            },
        ],
        "tool_results": {"c1": '{"ok": true, "entries": ["notes.txt"]}'},
    }
    body.update(changes)
    return body


def _write(tmp_path: Path, body: dict[str, Any]) -> Path:
    path = tmp_path / "hand.cassette.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _scenario(cassette: Path, **extra: Any) -> Scenario:
    data: dict[str, Any] = {
        "id": "hand/replay",
        "case": "HAND-REPLAY",
        "mode": "replay",
        "messages": ["list the workspace"],
        "cassette": str(cassette),
        "assertions": {
            "turn_completed": True,
            "no_errors": True,
            "required_tools": ["list_dir"],
            "response_contains": "Found it.",
        },
    }
    data.update(extra)
    return Scenario.model_validate(data)


async def test_a_hand_written_cassette_replays(tmp_path: Path) -> None:
    run = await run_scenario(_scenario(_write(tmp_path, _cassette_body())))

    kinds = [event_type(event) for event in run.events]
    assert "ToolCallStarted" in kinds
    assert kinds[-1] == "TurnComplete"
    assert [call.name for call in run.calls] == ["list_dir"]
    assert run.view.provider_calls == 2
    assert run.view.script_remaining == 0
    deltas = "".join(
        str(event_field(event, "delta"))
        for event in run.events
        if event_type(event) == "AssistantDelta"
    )
    assert deltas == "Looking.Found it."


async def test_the_recorded_result_answers_the_recording_executor(tmp_path: Path) -> None:
    run = await run_scenario(_scenario(_write(tmp_path, _cassette_body())))
    results = [event for event in run.events if event_type(event) == "ToolCallResult"]
    assert [json.loads(str(event_field(event, "result"))) for event in results] == [
        {"ok": True, "entries": ["notes.txt"]}
    ]


async def test_the_real_executor_runs_the_tool_on_the_fixture(tmp_path: Path) -> None:
    scenario = _scenario(
        _write(tmp_path, _cassette_body()),
        executor="real",
        workspace_fixture="drift_ws",
        assertions={"turn_completed": True, "no_errors": True, "max_tool_errors": 0},
    )
    run = await run_scenario(scenario)
    results = [event for event in run.events if event_type(event) == "ToolCallResult"]
    assert len(results) == 1
    assert event_field(results[0], "error") in (None, "")


async def test_a_renamed_tool_argument_is_stale(tmp_path: Path) -> None:
    body = _cassette_body()
    body["calls"][0]["events"][1]["args"] = {"directory": "."}
    with pytest.raises(CassetteStaleError) as caught:
        await run_scenario(_scenario(_write(tmp_path, body)))
    assert str(caught.value) == "cassette_stale: tool list_dir changed"


async def test_a_removed_tool_is_stale(tmp_path: Path) -> None:
    body = _cassette_body()
    body["calls"][0]["events"][1]["name"] = "teleport"
    with pytest.raises(CassetteStaleError, match=r"cassette_stale: tool teleport changed"):
        await run_scenario(_scenario(_write(tmp_path, body)))


async def test_staleness_in_a_later_call_fails_too(tmp_path: Path) -> None:
    body = _cassette_body()
    body["calls"][1]["events"].insert(
        0,
        {"type": "tool_call", "call_id": "c2", "name": "read_file", "args": {"old_arg": "x"}},
    )
    with pytest.raises(CassetteStaleError, match=r"tool read_file changed"):
        await run_scenario(_scenario(_write(tmp_path, body)))


async def test_a_changed_prompt_digest_only_warns(tmp_path: Path) -> None:
    body = _cassette_body(prompt_digest="0" * 64)
    with pytest.warns(CassetteDigestWarning, match="prompt_digest differs"):
        run = await run_scenario(_scenario(_write(tmp_path, body)))
    assert run.view.provider_calls == 2


async def test_a_changed_tool_schema_digest_only_warns(tmp_path: Path) -> None:
    body = _cassette_body(tool_schema_digest="f" * 64)
    with pytest.warns(CassetteDigestWarning, match="tool_schema_digest differs"):
        await run_scenario(_scenario(_write(tmp_path, body)))


async def test_no_digests_means_no_warning(tmp_path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", CassetteDigestWarning)
        await run_scenario(_scenario(_write(tmp_path, _cassette_body())))


async def test_a_diverging_run_fails_instead_of_getting_an_empty_reply(tmp_path: Path) -> None:
    body = _cassette_body()
    body["calls"].pop()
    with pytest.raises(AssertionError):
        await run_scenario(_scenario(_write(tmp_path, body)))


def test_the_scripted_copy_carries_the_cassette(tmp_path: Path) -> None:
    path = _write(tmp_path, _cassette_body())
    scenario = _scenario(path)
    scripted = scripted_scenario(scenario, load_cassette(path))
    assert scripted.mode == "scripted"
    assert len(scripted.script or []) == 2
    assert scripted.tool_results == {"c1": {"ok": True, "entries": ["notes.txt"]}}
    assert scenario.mode == "replay" and scenario.script is None


def test_cassette_paths_resolve_from_the_repo_root_or_the_yaml_directory(tmp_path: Path) -> None:
    path = _write(tmp_path, _cassette_body())
    assert resolve_cassette(_scenario(path)) == path
    with pytest.raises(ReplayDriverError, match="cassette not found"):
        resolve_cassette(_scenario(tmp_path / "missing.cassette.json"))
    relative = _scenario(Path("regression/t3/not_here.cassette.json"))
    with pytest.raises(ReplayDriverError, match="not found"):
        resolve_cassette(relative)


async def test_the_realtime_driver_does_not_replay(tmp_path: Path) -> None:
    scenario = _scenario(_write(tmp_path, _cassette_body()), driver="realtime")
    with pytest.raises(ReplayDriverError, match="loop or gateway"):
        await run_scenario(scenario)


def test_the_plugin_collects_a_trajectory_yaml_and_replays_it(
    pytester: pytest.Pytester,
) -> None:
    folder = pytester.path / "regression" / "t3" / "trajectories" / "hand"
    folder.mkdir(parents=True)
    cassette = folder / "replay_hand.cassette.json"
    cassette.write_text(json.dumps(_cassette_body()), encoding="utf-8")
    (folder / "replay_hand.yaml").write_text(
        "\n".join(
            [
                "case: T3-HAND-01",
                "mode: replay",
                "messages:",
                "- list the workspace",
                f"cassette: {cassette}",
                "assertions:",
                "  turn_completed: true",
                "  response_contains: Found it.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    # A cassette next to its YAML must not be collected as a scenario itself.
    result = pytester.runpytest_inprocess("-p", "regression.plugin", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_a_cassette_loads_as_the_model(tmp_path: Path) -> None:
    loaded = load_cassette(_write(tmp_path, _cassette_body()))
    assert isinstance(loaded, Cassette)
    assert loaded.calls[0].events[0].type == "thinking"
