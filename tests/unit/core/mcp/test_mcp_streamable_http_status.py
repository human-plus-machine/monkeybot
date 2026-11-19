"""Streamable HTTP connect must fail fast when initialize returns an HTTP error.

A 401/403/5xx is raised on the SDK's background POST task. That cancels the
session owner; the status shows up only when the owner's stack closes. The
connect call has to surface that status instead of waiting out the read timeout.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from monkeybot.core.mcp.mcp_client import MCP_STATUS_FAILED, MCPClient, MCPConnectionError


def _http_response(status: int, reason: str, body: bytes) -> bytes:
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode() + body


async def _serve(status: int, reason: str, *, stall: bool = False) -> tuple[asyncio.Server, int]:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with suppress(Exception):
            await asyncio.wait_for(reader.read(65536), 2)
        try:
            if stall:
                # Stay open until the client aborts. A fixed sleep would pin
                # server.wait_closed after the connect task is cancelled.
                await reader.read(1)
                return
            writer.write(_http_response(status, reason, b'{"message":"no"}'))
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    sockets = server.sockets or []
    port = sockets[0].getsockname()[1]
    return server, port


def _session_tasks() -> list[asyncio.Task[object]]:
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("mcp-session-") and not task.done()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, "Unauthorized"), (403, "Forbidden"), (500, "Internal Server Error")],
)
async def test_http_error_on_initialize_fails_the_connect(status: int, reason: str) -> None:
    server, port = await _serve(status, reason)
    client = MCPClient()
    try:
        with pytest.raises(MCPConnectionError, match=f"HTTP {status}") as info:
            await asyncio.wait_for(
                client.connect_streamable_http(
                    "gateway",
                    f"http://127.0.0.1:{port}/mcp",
                    headers={"X-API-Key": "bad"},
                ),
                5,
            )
    finally:
        server.close()
        await server.wait_closed()
    assert info.value.server_name == "gateway"
    recorded = client.status("gateway")
    assert recorded["status"] == MCP_STATUS_FAILED  # type: ignore[index]
    assert f"HTTP {status}" in recorded["error"]  # type: ignore[index]
    assert _session_tasks() == []


@pytest.mark.asyncio
async def test_http_404_on_initialize_is_session_terminated() -> None:
    """404 is the one status the SDK turns into a JSON-RPC error for the waiter."""
    server, port = await _serve(404, "Not Found")
    client = MCPClient()
    try:
        with pytest.raises(MCPConnectionError, match="Session terminated"):
            await asyncio.wait_for(
                client.connect_streamable_http("missing", f"http://127.0.0.1:{port}/mcp"),
                5,
            )
    finally:
        server.close()
        await server.wait_closed()
    assert client.status("missing")["status"] == MCP_STATUS_FAILED  # type: ignore[index]
    assert _session_tasks() == []


@pytest.mark.asyncio
async def test_cancelling_a_hung_streamable_connect_ends_the_owner() -> None:
    server, port = await _serve(0, "", stall=True)
    client = MCPClient()
    task = asyncio.create_task(
        client.connect_streamable_http("slow", f"http://127.0.0.1:{port}/mcp"),
        name="cancel-http-connect",
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _session_tasks() == []
    finally:
        server.close()
        await server.wait_closed()
