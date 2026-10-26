"""Streaming SSE reader: frames arrive before the response ends."""

from __future__ import annotations

import asyncio
import json

from testkit.sse import SseFrame, SseParser, StreamingAsgiResponse, read_sse_until


def test_parser_reads_id_data_and_comment_across_chunks() -> None:
    parser = SseParser()
    assert parser.feed(b": pi") == []
    frames = parser.feed(
        b"ng 1\r\n\r\n"
        b"id: 1\n"
        b'data: {"type":"AssistantDelta","delta":"hi"}\n\n'
        b'data: {"type":"ActiveRequests","request_ids":[]}\n\n'
    )
    assert frames == [
        SseFrame(comment="ping 1"),
        SseFrame(id="1", data='{"type":"AssistantDelta","delta":"hi"}'),
        SseFrame(data='{"type":"ActiveRequests","request_ids":[]}'),
    ]


async def test_reader_stops_before_the_response_ends() -> None:
    """A frame that matches is returned while the ASGI app is still sending.

    ``httpx.ASGITransport`` buffers until ``more_body`` is false, so this app
    would never return through that transport: it holds the body open.
    """
    hold = asyncio.Event()
    reached_hold = asyncio.Event()
    finished = asyncio.Event()
    head = (
        b": ping 1\n\n"
        b'id: 1\ndata: {"type":"AssistantDelta","delta":"hi"}\n\n'
        b'id: 2\ndata: {"type":"TurnComplete"}\n\n'
    )
    tail = b'id: 9\ndata: {"type":"After"}\n\n'

    async def app(scope: dict[str, object], receive: object, send: object) -> None:
        del receive
        if scope["type"] != "http":
            return
        sender = send
        assert callable(sender)
        await sender({"type": "http.response.start", "status": 200, "headers": []})
        await sender({"type": "http.response.body", "body": head, "more_body": True})
        reached_hold.set()
        await hold.wait()
        await sender({"type": "http.response.body", "body": tail, "more_body": True})
        finished.set()

    async with StreamingAsgiResponse(app, method="GET", path="/events") as stream:
        frames = await read_sse_until(
            stream,
            lambda frame: _frame_type(frame) == "TurnComplete",
        )

    assert reached_hold.is_set()
    assert not finished.is_set()
    assert [frame.comment for frame in frames] == ["ping 1", None, None]
    assert [_frame_type(frame) for frame in frames] == [
        None,
        "AssistantDelta",
        "TurnComplete",
    ]
    assert all(_frame_type(frame) != "After" for frame in frames)


def _frame_type(frame: SseFrame) -> str | None:
    if frame.data is None:
        return None
    payload = json.loads(frame.data)
    kind = payload.get("type")
    return kind if isinstance(kind, str) else None
