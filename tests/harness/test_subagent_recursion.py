"""Unit tests for SubagentRecursionMW."""

from __future__ import annotations

import pytest

from src.core.harness.errors import RecursionBudgetExceeded
from src.core.harness.middleware.subagent_recursion import SubagentRecursionMW


def test_depth_bound_raises() -> None:
    mw = SubagentRecursionMW(depth_limit=2)
    with mw.enter():
        assert mw.current_depth() == 1
        with mw.enter():
            assert mw.current_depth() == 2
            with pytest.raises(RecursionBudgetExceeded) as exc:
                with mw.enter():
                    pass
            assert exc.value.depth == 3
            assert exc.value.limit == 2
    assert mw.current_depth() == 0
