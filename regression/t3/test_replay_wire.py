"""Replay the Ollama wire cassettes through the real SDK and adapter.

``httpx.MockTransport`` serves the recorded bytes. The test patches
``_keepalive_http_client`` so ``OllamaProvider`` uses that client. No source
change, and the socket cannot connect.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest

from regression.t3.tools.check_staleness import mismatches
from regression.t3.tools.record_wire import (
    MODEL,
    THINKING_ONLY,
    TOOL_TURN,
    WIRE_DIR,
    WireScenario,
    event_to_wire,
)

_DUMMY_KEY = "ollama-replay-dummy"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replay that opens a socket fails here instead of reaching the network."""

    def refuse(self: socket.socket, address: object) -> NoReturn:
        raise AssertionError(f"replay tried to connect to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def _load(scenario: WireScenario) -> dict[str, Any]:
    path = WIRE_DIR / scenario.filename
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _tools(scenario: WireScenario) -> list[Any]:
    from monkeybot.core.types.types_tools import ToolDef

    if scenario.tool_name is None:
        return []
    return [
        ToolDef(
            name=scenario.tool_name,
            description="Add two integers.",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            },
        )
    ]


async def _replay(scenario: WireScenario, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from monkeybot.core.llm.provider import Message
    from monkeybot.providers._sdk_client import aclose_sdk_clients
    from monkeybot.providers.ollama import OllamaProvider

    cassette = _load(scenario)
    seen: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        response = cassette["response"]
        return httpx.Response(
            int(response["status"]),
            headers=dict(response["headers"]),
            content="".join(response["chunks"]).encode("utf-8"),
        )

    def factory(*, base_url: str | None) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"transport": httpx.MockTransport(handler)}
        if base_url is not None:
            kwargs["base_url"] = base_url
        return httpx.AsyncClient(**kwargs)

    monkeypatch.setenv("OLLAMA_API_KEY", _DUMMY_KEY)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setattr("monkeybot.providers._openai_compat._keepalive_http_client", factory)
    await aclose_sdk_clients()
    provider = OllamaProvider(
        mode="cloud",
        temperature=scenario.temperature,
        max_tokens=scenario.max_tokens,
        thinking_budget=scenario.thinking_budget,
        reasoning_effort=scenario.reasoning_effort,
    )
    events = [
        event_to_wire(event)
        async for event in provider.stream(
            [Message.text("user", scenario.prompt)],
            _tools(scenario),
            model=MODEL,
        )
    ]
    assert seen, "adapter made no HTTP request"
    method, path, raw_body = seen[0]
    request = cassette["request"]
    assert method == request["method"]
    assert path == request["path"]
    assert path.endswith("/chat/completions")
    assert json.loads(raw_body) == request["body"]
    assert events == cassette["expected_events"]
    assert events[-1]["type"] == "done"
    return cassette


@pytest.mark.case("T3-PROV-07")
async def test_ollama_tool_turn_replays(monkeypatch: pytest.MonkeyPatch) -> None:
    cassette = await _replay(TOOL_TURN, monkeypatch)
    calls = [
        event
        for event in cassette["expected_events"]
        if event["type"] == "tool_call" and event["name"] == "add"
    ]
    assert calls, cassette["expected_events"]
    body = cassette["request"]["body"]
    assert body["model"] == MODEL
    names = [tool["function"]["name"] for tool in body["tools"]]
    assert names == ["add"]


@pytest.mark.case("T3-PROV-06")
async def test_ollama_thinking_only_replays(monkeypatch: pytest.MonkeyPatch) -> None:
    cassette = await _replay(THINKING_ONLY, monkeypatch)
    types = [event["type"] for event in cassette["expected_events"]]
    assert "thinking" in types
    assert "text" not in types
    assert "tool_call" not in types
    assert "tools" not in cassette["request"]["body"]


@pytest.mark.case("T3-PROV-08")
def test_sdk_version_matches_cassettes(tmp_path: Path) -> None:
    assert not mismatches(), mismatches()
    source = WIRE_DIR / TOOL_TURN.filename
    copied = json.loads(source.read_text(encoding="utf-8"))
    copied["sdk"]["openai"] = "0.0.0-stale"
    target = tmp_path / "stale.json"
    target.write_text(json.dumps(copied), encoding="utf-8")
    found = mismatches(tmp_path)
    assert found and "0.0.0-stale" in found[0]
