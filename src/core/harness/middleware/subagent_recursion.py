"""SubagentRecursionMW — bound recursion depth per SubagentSpec.recursion_depth_limit."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from ..errors import RecursionBudgetExceeded

_DEPTH: ContextVar[int] = ContextVar("harness_subagent_depth", default=0)


class SubagentRecursionMW:
    name = "SubagentRecursionMW"

    def __init__(self, *, depth_limit: int = 3) -> None:
        self.depth_limit = depth_limit

    def current_depth(self) -> int:
        return _DEPTH.get()

    @contextmanager
    def enter(self) -> Iterator[int]:
        depth = _DEPTH.get() + 1
        if depth > self.depth_limit:
            raise RecursionBudgetExceeded(depth=depth, limit=self.depth_limit)
        token = _DEPTH.set(depth)
        try:
            yield depth
        finally:
            _DEPTH.reset(token)
