"""Push-to-talk key gate for the realtime CLI.

Hold the configured modifier (Command on macOS by default) to unmute the
microphone. Requires ``pynput`` (installed via ``monkeybot[cli-realtime]``).
On macOS, grant Accessibility permission to the terminal app if key events
are not detected.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

try:
    from pynput import keyboard

    _HAS_PYNPUT = True
except ImportError:
    keyboard = None
    _HAS_PYNPUT = False


class PushToTalkError(Exception):
    """Push-to-talk setup failed."""


class PushToTalkGate:
    """Tracks whether the push-to-talk key is currently held."""

    def __init__(self, *, key_name: str = "cmd") -> None:
        if not _HAS_PYNPUT:
            raise PushToTalkError(
                "pynput is required for push-to-talk. Install with: uv sync --extra cli-realtime"
            )
        self._key_name = key_name.lower().strip()
        self._held = False
        self._lock = threading.Lock()
        self._listener: Any | None = None
        self._target_keys = self._resolve_keys(self._key_name)

    @staticmethod
    def _resolve_keys(key_name: str) -> set[Any]:
        assert keyboard is not None
        aliases: dict[str, set[Any]] = {
            "cmd": {keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r},
            "command": {keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r},
            "meta": {keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r},
            "alt": {keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r},
            "option": {keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r},
            "ctrl": {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r},
            "control": {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r},
            "space": {keyboard.Key.space},
        }
        keys = aliases.get(key_name)
        if keys is None:
            raise PushToTalkError(
                f"Unsupported push-to-talk key '{key_name}'. "
                "Use one of: cmd, alt, ctrl, space"
            )
        return keys

    @property
    def key_label(self) -> str:
        labels = {
            "cmd": "⌘ Command",
            "command": "⌘ Command",
            "meta": "⌘ Command",
            "alt": "⌥ Option",
            "option": "⌥ Option",
            "ctrl": "⌃ Control",
            "control": "⌃ Control",
            "space": "Space",
        }
        return labels.get(self._key_name, self._key_name)

    def is_held(self) -> bool:
        with self._lock:
            return self._held

    def _on_press(self, key: Any) -> None:
        if key in self._target_keys:
            with self._lock:
                was_held = self._held
                self._held = True
            if not was_held:
                logger.debug("push-to-talk pressed (%s)", self._key_name)

    def _on_release(self, key: Any) -> None:
        if key in self._target_keys:
            with self._lock:
                was_held = self._held
                self._held = False
            if was_held:
                logger.debug("push-to-talk released (%s)", self._key_name)

    def start(self) -> None:
        assert keyboard is not None
        if self._listener is not None:
            return
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.daemon = True
        try:
            self._listener.start()
        except Exception as exc:
            self._listener = None
            raise PushToTalkError(
                f"Failed to start push-to-talk listener: {exc}. "
                "On macOS, grant Accessibility permission to your terminal app "
                "(System Settings → Privacy & Security → Accessibility)."
            ) from exc
        logger.info("push-to-talk ready: hold %s to speak", self.key_label)

    def stop(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                logger.exception("push-to-talk listener stop failed")
        with self._lock:
            self._held = False
