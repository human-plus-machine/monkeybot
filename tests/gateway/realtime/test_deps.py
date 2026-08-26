"""Tests for realtime dependency container."""

from __future__ import annotations

import pytest

from monkeybot.gateway.realtime.deps import RealtimeDependencies


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


def test_sync_live_slices_does_not_touch_provider_or_storage() -> None:
    deps = RealtimeDependencies()
    sentinel_provider = object()
    deps.realtime_provider = sentinel_provider  # type: ignore[assignment]
    deps.freeze()

    deps.sync_live_slices(_FakeGatewayRuntime())

    assert deps.realtime_provider is sentinel_provider
