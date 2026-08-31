"""Tests for realtime dependency container."""

from __future__ import annotations

import pytest

from monkeybot.gateway.realtime.deps import RealtimeDependencies
from monkeybot.gateway.realtime.routes import _live_slices
from monkeybot.gateway.sse.app import gateway_runtime


def test_freeze_blocks_mutation() -> None:
    deps = RealtimeDependencies()
    deps.storage = None
    deps.freeze()
    with pytest.raises(RuntimeError, match="frozen"):
        deps.storage = None


def test_live_slices_read_through_gateway_runtime_when_mcp_shared() -> None:
    mcp = object()
    prev = gateway_runtime.mcp
    prev_inspectors = gateway_runtime.inspectors
    try:
        gateway_runtime.mcp = mcp  # type: ignore[assignment]
        gateway_runtime.inspectors = ["fresh"]  # type: ignore[list-item]
        deps = RealtimeDependencies()
        deps.mcp = mcp  # type: ignore[assignment]
        deps.inspectors = ["stale"]
        live = _live_slices(deps)
        assert live is gateway_runtime
        assert live.inspectors == ["fresh"]
    finally:
        gateway_runtime.mcp = prev
        gateway_runtime.inspectors = prev_inspectors


def test_live_slices_keep_deps_when_mcp_is_not_shared() -> None:
    deps = RealtimeDependencies()
    deps.inspectors = ["only-deps"]
    assert _live_slices(deps) is deps
    assert _live_slices(deps).inspectors == ["only-deps"]
