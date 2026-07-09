"""Tests for push-to-talk key gate."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from monkeybot.cli.push_to_talk import PushToTalkError, PushToTalkGate


def test_resolve_cmd_keys() -> None:
    with patch("monkeybot.cli.push_to_talk._HAS_PYNPUT", True), patch(
        "monkeybot.cli.push_to_talk.keyboard"
    ) as kb:
        kb.Key.cmd = object()
        kb.Key.cmd_l = object()
        kb.Key.cmd_r = object()
        gate = PushToTalkGate(key_name="cmd")
        assert gate.key_label == "⌘ Command"
        assert not gate.is_held()


def test_unsupported_key_raises() -> None:
    with patch("monkeybot.cli.push_to_talk._HAS_PYNPUT", True), patch(
        "monkeybot.cli.push_to_talk.keyboard"
    ):
        with pytest.raises(PushToTalkError, match="Unsupported"):
            PushToTalkGate(key_name="f13")


def test_press_release_toggles_held() -> None:
    with patch("monkeybot.cli.push_to_talk._HAS_PYNPUT", True), patch(
        "monkeybot.cli.push_to_talk.keyboard"
    ) as kb:
        cmd = object()
        kb.Key.cmd = cmd
        kb.Key.cmd_l = object()
        kb.Key.cmd_r = object()
        gate = PushToTalkGate(key_name="cmd")
        gate._on_press(cmd)
        assert gate.is_held()
        gate._on_release(cmd)
        assert not gate.is_held()


def test_missing_pynput_raises() -> None:
    with patch("monkeybot.cli.push_to_talk._HAS_PYNPUT", False):
        with pytest.raises(PushToTalkError, match="pynput"):
            PushToTalkGate(key_name="cmd")


def test_start_and_stop() -> None:
    with patch("monkeybot.cli.push_to_talk._HAS_PYNPUT", True), patch(
        "monkeybot.cli.push_to_talk.keyboard"
    ) as kb:
        kb.Key.cmd = object()
        kb.Key.cmd_l = object()
        kb.Key.cmd_r = object()
        listener = MagicMock()
        kb.Listener.return_value = listener
        gate = PushToTalkGate(key_name="cmd")
        gate.start()
        listener.start.assert_called_once()
        gate.stop()
        listener.stop.assert_called_once()
        assert not gate.is_held()
