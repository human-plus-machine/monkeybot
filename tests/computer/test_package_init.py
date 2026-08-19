"""Tests for the ``computer`` package's env gate and tool registry."""

from __future__ import annotations

import sys

import pytest

from monkeybot import computer


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MONKEYBOT_COMPUTER_TOOLS", raising=False)


class TestComputerToolsEnabledFromEnv:
    def test_default_off(self) -> None:
        assert computer.computer_tools_enabled_from_env() is False

    @pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
    def test_on_values(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONKEYBOT_COMPUTER_TOOLS", value)
        assert computer.computer_tools_enabled_from_env() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "garbage"])
    def test_off_values(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONKEYBOT_COMPUTER_TOOLS", value)
        assert computer.computer_tools_enabled_from_env() is False


class TestShouldEnableComputerTools:
    def test_requires_both_env_and_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONKEYBOT_COMPUTER_TOOLS", "1")
        monkeypatch.setattr(sys, "platform", "linux")
        assert computer.should_enable_computer_tools() is False

    def test_true_on_darwin_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONKEYBOT_COMPUTER_TOOLS", "1")
        monkeypatch.setattr(sys, "platform", "darwin")
        assert computer.should_enable_computer_tools() is True

    def test_false_when_disabled_even_on_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        assert computer.should_enable_computer_tools() is False


class TestToolRegistry:
    def test_build_computer_tools_returns_one_per_name(self) -> None:
        tools = computer.build_computer_tools()
        names = {t.tool_def.name for t in tools}
        assert names == computer.COMPUTER_TOOL_NAMES
        assert len(tools) == len(computer.COMPUTER_TOOL_NAMES)

    def test_every_tool_name_is_prefixed(self) -> None:
        assert all(name.startswith("computer_") for name in computer.COMPUTER_TOOL_NAMES)

    def test_always_scope_keys_are_a_subset_of_tool_names(self) -> None:
        assert set(computer.ALWAYS_SCOPE) <= computer.COMPUTER_TOOL_NAMES

    def test_mutating_tools_excluded_from_always_scope(self) -> None:
        assert "computer_move" not in computer.ALWAYS_SCOPE
        assert "computer_trash" not in computer.ALWAYS_SCOPE

    def test_is_computer_tool_name(self) -> None:
        assert computer.is_computer_tool_name("computer_open")
        assert not computer.is_computer_tool_name("write_file")
