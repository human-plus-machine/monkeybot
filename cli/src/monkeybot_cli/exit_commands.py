"""Shared exit-command parsing for chat and talk."""

from __future__ import annotations

_EXIT_COMMANDS = frozenset({"/bye", "/quit", "/exit"})


def is_exit_command(line: str) -> bool:
    """Return True for /bye, /quit, /exit (optional trailing punctuation)."""
    token = line.strip().lower().split(maxsplit=1)[0] if line.strip() else ""
    while token and token[-1] in ".,!;:":
        token = token[:-1]
    return token in _EXIT_COMMANDS
