"""Optional per-stream timestamps for profiling.

The OpenAI-compatible adapter buffers tool-argument fragments and yields one
``ToolCall`` after the HTTP stream ends. A profiler that only sees that event
cannot tell a first token from the time spent writing arguments. Binding a
:class:`StreamMarks` around a stream records when content chunks actually
arrived. With nothing bound, the mark calls are a no-op.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass

_current: ContextVar[StreamMarks | None] = ContextVar("mb_stream_marks", default=None)


@dataclass
class StreamMarks:
    """Chunk arrival times, relative to ``started`` (``time.perf_counter``)."""

    started: float
    first_at: float | None = None
    last_at: float | None = None
    first_kind: str | None = None
    arg_first_at: float | None = None
    arg_last_at: float | None = None
    arg_chars: int = 0


def bind_stream_marks(marks: StreamMarks) -> Token[StreamMarks | None]:
    return _current.set(marks)


def reset_stream_marks(token: Token[StreamMarks | None]) -> None:
    _current.reset(token)


@contextmanager
def suppress_stream_marks() -> Iterator[None]:
    """Hide marks bound on this task.

    ``asyncio.create_task`` copies the current context, so a summary started
    during a profiled turn would otherwise write the user stream's marks.
    """
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)


def mark_content(kind: str) -> None:
    """Record a text, thinking, or tool-name chunk from the model."""
    marks = _current.get()
    if marks is None:
        return
    now = time.perf_counter()
    if marks.first_at is None:
        marks.first_at = now
        marks.first_kind = kind
    marks.last_at = now


def mark_tool_args(nchars: int) -> None:
    """Record one tool-argument fragment. ``nchars`` is the fragment length."""
    if nchars <= 0:
        return
    marks = _current.get()
    if marks is None:
        return
    now = time.perf_counter()
    if marks.arg_first_at is None:
        marks.arg_first_at = now
    marks.arg_last_at = now
    marks.arg_chars += nchars
    if marks.first_at is None:
        marks.first_at = now
        marks.first_kind = "tool_args"
    marks.last_at = now


def marks_report(marks: StreamMarks) -> dict[str, float | int | str | None]:
    """Milliseconds from ``marks.started`` to the recorded chunk times."""

    def rel(at: float | None) -> float | None:
        if at is None:
            return None
        return round((at - marks.started) * 1000, 3)

    decode_span_ms = None
    if marks.first_at is not None and marks.last_at is not None:
        decode_span_ms = round((marks.last_at - marks.first_at) * 1000, 3)
    tool_arg_span_ms = None
    if marks.arg_first_at is not None and marks.arg_last_at is not None:
        tool_arg_span_ms = round((marks.arg_last_at - marks.arg_first_at) * 1000, 3)
    return {
        "first_chunk_ms": rel(marks.first_at),
        "last_chunk_ms": rel(marks.last_at),
        "first_chunk_kind": marks.first_kind,
        "decode_span_ms": decode_span_ms,
        "tool_arg_span_ms": tool_arg_span_ms,
        "tool_arg_chars": marks.arg_chars,
        "tool_arg_done_ms": rel(marks.arg_last_at),
    }
