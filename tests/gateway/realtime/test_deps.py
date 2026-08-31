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


class _FakeGatewayRuntime:
    """Minimal stand-in for ``gateway.sse.app.GatewayRuntime``."""

    def __init__(self) -> None:
        self.inspectors = ["new-inspector"]
        self.hook_manager = "new-hooks"
        self.web_search_tool = "new-search"
        self.run_command_allowed_commands = ["ls"]
        self.run_command_allowed_path_prefixes = ["./"]
        self.subagent_registry = {"helper": "new-persona"}
        self.computer_tools = ["new-tool"]
        self.computer_approvals_persist = "new-persist"


def test_sync_live_slices_updates_reloadable_fields_after_freeze() -> None:
    deps = RealtimeDependencies()
    deps.freeze()

    deps.sync_live_slices(_FakeGatewayRuntime())

    assert deps.inspectors == ["new-inspector"]
    assert deps.hook_manager == "new-hooks"
    assert deps.web_search_tool == "new-search"
    assert deps.run_command_allowed_commands == ["ls"]
    assert deps.subagent_registry == {"helper": "new-persona"}
    assert deps.computer_tools == ["new-tool"]
    assert deps.computer_approvals_persist == "new-persist"
    # Still frozen for every other field.
    with pytest.raises(RuntimeError, match="frozen"):
        deps.storage = None


def test_sync_live_slices_rebuilds_provider_and_leaves_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = RealtimeDependencies()
    sentinel_storage = object()
    deps.storage = sentinel_storage  # type: ignore[assignment]
    old_provider = object()
    deps.realtime_provider = old_provider  # type: ignore[assignment]
    deps.freeze()

    rebuilt = object()
    monkeypatch.setattr(
        "monkeybot.providers.gemini_live.GeminiLiveProvider",
        lambda *args, **kwargs: rebuilt,
    )

    deps.sync_live_slices(_FakeGatewayRuntime())

    assert deps.realtime_provider is rebuilt
    assert deps.storage is sentinel_storage


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
