"""Shared in-process gateway helpers for the P2-04 cases.

The YAML driver posts one ``/reply`` per message. These helpers drive the
other routes and read SSE frames as they arrive.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from monkeybot.core.llm.provider import Message
from monkeybot.core.runtime.history_rewrite import resolve_active_thread_id
from monkeybot.core.types.content_blocks import Text
from testkit.gateway import GatewaySession
from testkit.sse import SseFrame, SseParser

_FRAME_TIMEOUT_S = 20.0


def payload(frame: SseFrame) -> dict[str, Any]:
    """JSON body of one SSE frame, plus its id when the gateway sent one."""
    if frame.data is None:
        comment = frame.comment or ""
        kind = "ping" if comment.startswith("ping") else "comment"
        return {"type": kind, "comment": comment}
    try:
        parsed = json.loads(frame.data)
    except json.JSONDecodeError:
        parsed = {"type": "data", "data": frame.data}
    if not isinstance(parsed, dict):
        parsed = {"type": "data", "data": parsed}
    if frame.id:
        parsed = {**parsed, "id": frame.id}
    return parsed


def error_code(response: httpx.Response) -> str:
    body = response.json()
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else ""


class FrameReader:
    """Pull parsed SSE frames from one live ``GET /events`` body."""

    def __init__(self, stream: AsyncIterator[bytes]) -> None:
        self._chunks = stream.__aiter__()
        self._parser = SseParser()
        self._pending: list[SseFrame] = []

    async def next_frame(self, timeout: float = _FRAME_TIMEOUT_S) -> SseFrame:
        while not self._pending:
            chunk = await asyncio.wait_for(self._chunks.__anext__(), timeout)
            self._pending.extend(self._parser.feed(chunk))
        return self._pending.pop(0)

    async def until(
        self,
        kind: str | tuple[str, ...],
        timeout: float = _FRAME_TIMEOUT_S,
    ) -> list[dict[str, Any]]:
        """Read until a frame of ``kind`` arrives, including that frame."""
        wanted = (kind,) if isinstance(kind, str) else kind
        seen: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                kinds = [item.get("type") for item in seen]
                raise TimeoutError(f"timed out waiting for {wanted}; saw {kinds}")
            frame = await self.next_frame(timeout=remaining)
            body = payload(frame)
            seen.append(body)
            if body.get("type") in wanted:
                return seen

    async def collect(self, seconds: float) -> list[dict[str, Any]]:
        """Read whatever arrives before ``seconds`` elapse."""
        seen: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return seen
            try:
                frame = await self.next_frame(timeout=remaining)
            except TimeoutError:
                return seen
            seen.append(payload(frame))


@asynccontextmanager
async def listening(
    gateway: GatewaySession,
    session_id: str,
    *,
    last_event_id: str | None = None,
) -> AsyncIterator[FrameReader]:
    """Open ``GET /events`` and close it when the caller finishes."""
    stream, reader = await open_reader(gateway, session_id, last_event_id=last_event_id)
    try:
        yield reader
    finally:
        await stream.aclose()


def data_ids(frames: list[dict[str, Any]]) -> list[str]:
    """SSE ids of data frames, in arrival order."""
    found: list[str] = []
    for frame in frames:
        raw = frame.get("id")
        if isinstance(raw, str) and raw:
            found.append(raw)
    return found


async def open_reader(
    gateway: GatewaySession,
    session_id: str,
    *,
    last_event_id: str | None = None,
) -> tuple[Any, FrameReader]:
    stream = gateway.stream_events(session_id, last_event_id=last_event_id)
    await stream.__aenter__()
    return stream, FrameReader(stream)


async def wait_turn(gateway: GatewaySession, session_id: str) -> None:
    registry = getattr(gateway.app.state, "registry", None)
    bus = None if registry is None else registry.get(session_id)
    task = None if bus is None else bus.active_turn_task
    if task is not None:
        await task


async def history(gateway: GatewaySession, session_id: str) -> list[Message]:
    backend = gateway.app.state.storage
    thread_id = await resolve_active_thread_id(backend, session_id)
    rows = await backend.history().load(thread_id)
    return [row for row in rows if isinstance(row, Message)]


def message_text(message: Message) -> str:
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, Text):
            parts.append(block.text)
    return "".join(parts)


def user_row_id(rows: list[Message], text: str) -> str:
    for row in rows:
        if row.role == "user" and text in message_text(row) and row.row_id:
            return row.row_id
    raise AssertionError(f"no user row containing {text!r}")


async def reply_and_finish(
    gateway: GatewaySession,
    session_id: str,
    message: str,
    *,
    request_id: str,
    reader: FrameReader,
) -> list[dict[str, Any]]:
    """Post one reply on an already-open stream and read through TurnComplete."""
    await reader.until("ActiveRequests")
    response = await gateway.post_reply(session_id, request_id, message)
    if response.status_code != 200:
        raise AssertionError(f"POST /reply returned {response.status_code}: {response.text[:300]}")
    frames = await reader.until("TurnComplete")
    await wait_turn(gateway, session_id)
    return frames


def remote_client(gateway: GatewaySession, host: str) -> httpx.AsyncClient:
    """JSON client whose ASGI peer address is ``host`` (admin loopback checks)."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app, client=(host, 9)),
        base_url="http://gateway.test",
        timeout=30,
    )


__all__ = [
    "FrameReader",
    "data_ids",
    "error_code",
    "history",
    "listening",
    "message_text",
    "open_reader",
    "payload",
    "remote_client",
    "reply_and_finish",
    "user_row_id",
    "wait_turn",
]
