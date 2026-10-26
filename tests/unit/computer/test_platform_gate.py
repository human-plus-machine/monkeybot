"""T1-COMP-01: computer tools stay off every non-macOS platform."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from monkeybot.computer import should_enable_computer_tools


def _config(flag: str | None) -> SimpleNamespace:
    values: dict[str, str] = {}
    if flag is not None:
        values["MONKEYBOT_COMPUTER_TOOLS"] = flag
    return SimpleNamespace(env_values=values)


@pytest.mark.case("T1-COMP-01")
@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_should_enable_computer_tools_false_off_darwin(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.delenv("MONKEYBOT_COMPUTER_TOOLS", raising=False)
    assert should_enable_computer_tools() is False
    assert should_enable_computer_tools(None) is False

    monkeypatch.setenv("MONKEYBOT_COMPUTER_TOOLS", "1")
    assert should_enable_computer_tools() is False
    assert should_enable_computer_tools(None) is False
    for flag in ("1", "true", "yes", "on", "0", "false", "off"):
        assert should_enable_computer_tools(_config(flag)) is False
    assert should_enable_computer_tools(_config(None)) is False
