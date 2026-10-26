"""Streaming SSE reader.

``httpx.ASGITransport`` joins the body and returns only after ``more_body`` is
false. ``GET /events`` never ends, so a gateway client has to read frames as
the ASGI app sends them and stop on a predicate.
"""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

_ASGIApp = Callable[..., Awaitable[None]]
_PREDICATE = Callable[["SseFrame"], bool]


class SseStreamError(RuntimeError):
    """The ASGI app answered the stream with a non-200 status."""

    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self.body = body
        detail = f"SSE stream returned HTTP {status}"
        if body:
            detail = f"{detail}: {body}"
        super().__init__(detail)


@dataclass(frozen=True)
class SseFrame:
    """One SSE event, split into the fields the gateway writes."""

    id: str | None = None
    data: str | None = None
    comment: str | None = None
    event: str | None = None


def _field_value(raw: str) -> str:
    if raw.startswith(" "):
        return raw[1:]
    return raw


def _take_line(buf: str) -> tuple[str, str] | None:
    """Split one SSE line. A trailing ``\\r`` waits, in case ``\\n`` follows."""
    for index, char in enumerate(buf):
        if char == "\n":
            line = buf[:index]
            if line.endswith("\r"):
                line = line[:-1]
            return line, buf[index + 1 :]
        if char == "\r":
            if index + 1 >= len(buf):
                return None
            rest = index + 2 if buf[index + 1] == "\n" else index + 1
            return buf[:index], buf[rest:]
    return None


class SseParser:
    """Incremental parser. ``feed`` returns every frame the chunk completed."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buf = ""
        self._id: str | None = None
        self._event: str | None = None
        self._data: list[str] = []
        self._comments: list[str] = []
        self._touched = False

    def feed(self, chunk: bytes) -> list[SseFrame]:
        return self._consume(self._decoder.decode(chunk))

    def finish(self) -> list[SseFrame]:
        """Flush a trailing event when the stream closes without a blank line."""
        frames = self._consume(self._decoder.decode(b"", final=True))
        if self._buf:
            self._apply(self._buf)
            self._buf = ""
        dispatched = self._dispatch()
        if dispatched is not None:
            frames.append(dispatched)
        return frames

    def _consume(self, text: str) -> list[SseFrame]:
        if text:
            self._buf += text
        frames: list[SseFrame] = []
        while True:
            split = _take_line(self._buf)
            if split is None:
                return frames
            line, self._buf = split
            if line == "":
                dispatched = self._dispatch()
                if dispatched is not None:
                    frames.append(dispatched)
                continue
            self._apply(line)

    def _apply(self, line: str) -> None:
        if line.startswith(":"):
            self._touched = True
            self._comments.append(_field_value(line[1:]))
            return
        if ":" in line:
            name, raw = line.split(":", 1)
        else:
            name, raw = line, ""
        value = _field_value(raw)
        if name == "id":
            self._touched = True
            self._id = value
        elif name == "event":
            self._touched = True
            self._event = value
        elif name == "data":
            self._touched = True
            self._data.append(value)

    def _dispatch(self) -> SseFrame | None:
        if not self._touched:
            return None
        frame = SseFrame(
            id=self._id,
            data="\n".join(self._data) if self._data else None,
            comment="\n".join(self._comments) if self._comments else None,
            event=self._event,
        )
        self._id = None
        self._event = None
        self._data = []
        self._comments = []
        self._touched = False
        return frame


async def iter_sse_frames(chunks: AsyncIterator[bytes]) -> AsyncIterator[SseFrame]:
    """Yield frames as ``chunks`` arrive. The iterator may stay open."""
    parser = SseParser()
    async for chunk in chunks:
        for frame in parser.feed(chunk):
            yield frame
    for frame in parser.finish():
        yield frame


async def read_sse_until(
    chunks: AsyncIterator[bytes],
    predicate: _PREDICATE,
) -> list[SseFrame]:
    """Read frames until ``predicate`` matches one, including that frame.

    Returns the frames seen so far when the stream ends first. Does not read
    another chunk after the match, so a response that is still sending can be
    closed by the caller.
    """
    frames: list[SseFrame] = []
    parser = SseParser()
    async for chunk in chunks:
        for frame in parser.feed(chunk):
            frames.append(frame)
            if predicate(frame):
                return frames
    frames.extend(parser.finish())
    return frames


class StreamingAsgiResponse:
    """ASGI HTTP response that yields body chunks as ``send`` is called.

    The app task runs beside the reader. Closing the response cancels that
    task, which is how a client disconnects from ``GET /events``.
    """

    def __init__(
        self,
        app: _ASGIApp,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self._app = app
        self._method = method.upper()
        self._path = path
        self._headers = dict(headers or {})
        self._body = body
        self._queue: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue()
        self._started = asyncio.Event()
        self._disconnect = asyncio.Event()
        self._request_sent = False
        self._task: asyncio.Task[None] | None = None
        self.status: int | None = None

    async def __aenter__(self) -> StreamingAsgiResponse:
        self._task = asyncio.create_task(self._run(), name="sse-asgi")
        try:
            await asyncio.wait_for(self._started.wait(), timeout=30)
        except TimeoutError:
            await self.aclose()
            raise SseStreamError(0, "ASGI app did not start a response") from None
        if self.status != 200:
            detail = await self._drain_error_body()
            status = 500 if self.status is None else self.status
            await self.aclose()
            raise SseStreamError(status, detail)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        self._disconnect.set()
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            return

    async def _run(self) -> None:
        try:
            await self._app(self._scope(), self._receive, self._send)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._queue.put(exc)
        finally:
            await self._queue.put(None)
            if not self._started.is_set():
                self._started.set()

    def _scope(self) -> dict[str, Any]:
        raw_headers = [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in self._headers.items()
        ]
        path = self._path
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.5"},
            "http_version": "1.1",
            "method": self._method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": raw_headers,
            "client": ("127.0.0.1", 0),
            "server": ("gateway.test", 0),
            "root_path": "",
        }

    async def _receive(self) -> dict[str, Any]:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "http.response.start":
            self.status = int(message["status"])
            self._started.set()
            return
        if kind != "http.response.body":
            return
        chunk = message.get("body", b"")
        if chunk:
            await self._queue.put(bytes(chunk))
        if not message.get("more_body", False):
            await self._queue.put(None)

    async def _drain_error_body(self) -> str:
        parts: list[bytes] = []
        while len(parts) < 8:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1)
            except TimeoutError:
                break
            if item is None or isinstance(item, BaseException):
                break
            parts.append(item)
        return b"".join(parts).decode("utf-8", errors="replace")[:500]


__all__ = [
    "SseFrame",
    "SseParser",
    "SseStreamError",
    "StreamingAsgiResponse",
    "iter_sse_frames",
    "read_sse_until",
]
