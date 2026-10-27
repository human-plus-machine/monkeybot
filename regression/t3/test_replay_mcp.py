"""T3-MCP-01: a recorded ``tools/list`` + ``tools/call`` exchange replays through ``MCPClient``.

``regression/t3/mcp/tools_list_call.json`` holds what the MCP SDK session returned from a
local run of the fixture MCP server (stdio, no network, no model): the ``tools/list``
result, the ``tools/call`` request and result, and what ``MCPClient`` made of them
(prefixed tool names, input schemas, flattened result text).

Replay hands the same JSON to a real ``MCPClient`` through its ``hooks`` seam, parsed by the
SDK's own result models, so the prefixing and result normalization run for real and no
server starts. ``--update-golden`` re-records from the fixture server (refused when ``CI``
is set); without it, a fresh local run must equal the committed file.
"""

from __future__ import annotations

import json
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any, NoReturn

import pytest
from mcp.types import CallToolResult, ListToolsResult, ServerCapabilities

from monkeybot.core.mcp.mcp_client import MCPClient

_REPO = Path(__file__).resolve().parents[2]
_RECORDING = _REPO / "regression" / "t3" / "mcp" / "tools_list_call.json"
_SERVER_HOMES = (_REPO / "regression" / "fixtures" / "mcp" / "fixture_mcp_server.py",)
_SERVER_NAME = "fixture"
_CALL_NAME = "echo"
_CALL_ARGS = {"text": "ping"}
_DUMP: dict[str, Any] = {"mode": "json", "by_alias": True, "exclude_none": True}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replay that tries to open a connection fails loudly. A stdio child does not."""

    def refuse(self: socket.socket, address: object) -> NoReturn:
        raise AssertionError(f"replay tried to connect to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def _fixture_server() -> Path:
    for path in _SERVER_HOMES:
        if path.is_file():
            return path
    raise AssertionError("fixture MCP server not found in " + ", ".join(map(str, _SERVER_HOMES)))


def _load() -> dict[str, Any]:
    loaded = json.loads(_RECORDING.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


class _ReplaySession:
    """What ``ClientSession`` returns, taken from the recording and parsed by the SDK models."""

    def __init__(self, recording: dict[str, Any]) -> None:
        self._recording = recording
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def initialize(self) -> None:
        return None

    def get_server_capabilities(self) -> ServerCapabilities:
        return ServerCapabilities.model_validate(self._recording["server_capabilities"])

    async def list_tools(self, *args: Any, **kwargs: Any) -> ListToolsResult:
        assert not args and not kwargs, (
            f"recorded tools/list had no parameters, got {args} {kwargs}"
        )
        return ListToolsResult.model_validate(self._recording["tools_list"]["response"])

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any
    ) -> CallToolResult:
        recorded = self._recording["tools_call"]["request"]["params"]
        assert (name, dict(arguments or {})) == (recorded["name"], recorded["arguments"]), (
            f"tools/call {name!r} {arguments!r} was not recorded; "
            f"recorded {recorded['name']!r} {recorded['arguments']!r}"
        )
        assert not kwargs, f"recorded tools/call had no extra parameters, got {kwargs}"
        self.calls.append((name, dict(arguments or {})))
        return CallToolResult.model_validate(self._recording["tools_call"]["response"])


class _ReplayHooks:
    """``MCPStdioHooks`` that open no process: the session answers from the recording."""

    def __init__(self, recording: dict[str, Any]) -> None:
        self.session = _ReplaySession(recording)

    def stdio_server_parameters(
        self, command: str, args: list[str], env: dict[str, str] | None
    ) -> dict[str, Any]:
        return {"command": command, "args": args, "env": env}

    @asynccontextmanager
    async def stdio_client(self, params: object) -> AsyncIterator[tuple[str, str]]:
        yield ("replay-read", "replay-write")

    @asynccontextmanager
    async def client_session(self, read_stream: object, write_stream: object) -> AsyncIterator[Any]:
        yield self.session


def _outcome(client: MCPClient, tools: list[Any], result_text: str) -> dict[str, Any]:
    status = client.status(_SERVER_NAME)
    assert isinstance(status, dict)
    return {
        "tools": [
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            for tool in tools
        ],
        "capabilities": status["capabilities"],
        "result_text": result_text,
    }


@pytest.mark.case("T3-MCP-01")
async def test_the_recorded_exchange_gives_the_same_names_and_result_text() -> None:
    recording = _load()
    hooks = _ReplayHooks(recording)
    client = MCPClient(hooks=hooks)
    try:
        tools = await client.connect(_SERVER_NAME, "unused", ["unused"], {})
        text = await client.call_tool(_SERVER_NAME, _CALL_NAME, _CALL_ARGS)
        outcome = _outcome(client, tools, text)
    finally:
        await client.disconnect_all()

    assert [tool.name for tool in tools] == ["fixture__echo"]
    assert text == "echo: ping"
    assert outcome == recording["expected"]
    assert hooks.session.calls == [(_CALL_NAME, _CALL_ARGS)]


@pytest.mark.case("T3-MCP-01")
async def test_the_prefixed_name_splits_back_to_the_recorded_call() -> None:
    recording = _load()
    client = MCPClient(hooks=_ReplayHooks(recording))
    try:
        tools = await client.connect(_SERVER_NAME, "unused", ["unused"], {})
        split = client.split_prefixed_tool(tools[0].name)
    finally:
        await client.disconnect_all()
    assert split == (_SERVER_NAME, _CALL_NAME)


@pytest.mark.case("T3-MCP-01")
async def test_a_call_that_was_not_recorded_is_refused() -> None:
    client = MCPClient(hooks=_ReplayHooks(_load()))
    try:
        await client.connect(_SERVER_NAME, "unused", ["unused"], {})
        with pytest.raises(AssertionError, match="was not recorded"):
            await client.call_tool(_SERVER_NAME, _CALL_NAME, {"text": "something else"})
    finally:
        await client.disconnect_all()


class _TapeSession:
    """Wraps the SDK session and keeps what it returned, as JSON."""

    def __init__(self, session: Any, tape: dict[str, Any]) -> None:
        self._session = session
        self._tape = tape

    async def initialize(self) -> Any:
        return await self._session.initialize()

    def get_server_capabilities(self) -> Any:
        caps = self._session.get_server_capabilities()
        self._tape["server_capabilities"] = caps.model_dump(**_DUMP)
        return caps

    async def list_tools(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._session.list_tools(*args, **kwargs)
        self._tape["tools_list"] = {
            "request": {"method": "tools/list"},
            "response": result.model_dump(**_DUMP),
        }
        return result

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, **kw: Any) -> Any:
        result = await self._session.call_tool(name, arguments=arguments, **kw)
        self._tape["tools_call"] = {
            "request": {
                "method": "tools/call",
                "params": {"name": name, "arguments": dict(arguments or {})},
            },
            "response": result.model_dump(**_DUMP),
        }
        return result


class _TapeHooks:
    """The SDK's own stdio transport and session, with the session wrapped to keep a tape."""

    def __init__(self, tape: dict[str, Any]) -> None:
        self._tape = tape

    def stdio_server_parameters(
        self, command: str, args: list[str], env: dict[str, str] | None
    ) -> Any:
        from mcp import StdioServerParameters

        return StdioServerParameters(command=command, args=args, env=env)

    def stdio_client(self, params: Any) -> AbstractAsyncContextManager[tuple[Any, Any]]:
        from mcp.client.stdio import stdio_client

        return stdio_client(params)  # type: ignore[no-any-return]

    @asynccontextmanager
    async def client_session(self, read_stream: Any, write_stream: Any) -> AsyncIterator[Any]:
        from mcp import ClientSession

        async with ClientSession(read_stream, write_stream) as session:
            yield _TapeSession(session, self._tape)


async def _record() -> dict[str, Any]:
    """One local run of the fixture server through a real ``MCPClient``."""
    tape: dict[str, Any] = {}
    client = MCPClient(hooks=_TapeHooks(tape))
    try:
        tools = await client.connect(_SERVER_NAME, sys.executable, [str(_fixture_server())], {})
        text = await client.call_tool(_SERVER_NAME, _CALL_NAME, _CALL_ARGS)
        expected = _outcome(client, tools, text)
    finally:
        await client.disconnect_all()
    return {
        "schema_version": 1,
        "kind": "mcp",
        "server": _SERVER_NAME,
        "sdk": {"mcp": version("mcp")},
        **tape,
        "expected": expected,
    }


def _text(recording: dict[str, Any]) -> str:
    return json.dumps(recording, indent=2, ensure_ascii=False) + "\n"


@pytest.mark.case("T3-MCP-01")
async def test_the_recording_matches_a_fresh_local_run(request: pytest.FixtureRequest) -> None:
    fresh = _text(await _record())
    if request.config.getoption("update_golden"):
        _RECORDING.parent.mkdir(parents=True, exist_ok=True)
        _RECORDING.write_text(fresh, encoding="utf-8")
        return
    assert _RECORDING.is_file(), "missing tools_list_call.json; re-run with --update-golden"
    assert _RECORDING.read_text(encoding="utf-8") == fresh
