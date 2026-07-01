"""Typed outcomes from a scheduled-loop tick invocation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TickInvokeStatus(StrEnum):
    """Result of one scheduler tick invocation."""

    OK = "ok"
    SESSION_BUSY = "session_busy"
    ERROR = "error"


@dataclass(frozen=True)
class TickInvokeResult:
    """Outcome passed from :class:`TickInvoker` to the scheduler engine."""

    status: TickInvokeStatus
    error: str | None = None

    @classmethod
    def ok(cls) -> TickInvokeResult:
        return cls(status=TickInvokeStatus.OK)

    @classmethod
    def session_busy(cls) -> TickInvokeResult:
        return cls(status=TickInvokeStatus.SESSION_BUSY)

    @classmethod
    def fail(cls, message: str) -> TickInvokeResult:
        return cls(status=TickInvokeStatus.ERROR, error=message)
