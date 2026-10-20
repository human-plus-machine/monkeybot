"""Session-scoped record of which workspace files the agent has seen.

Coding mode edits only files the model has read (``read_before_write``) and whose
bytes have not changed since (``stale_read``). A stamp is ``(mtime_ns, size)`` so a
same-tick rewrite that changes length is still caught. The agent's own successful
writes refresh the stamp: it knows what it just wrote and need not re-read it.

The executor is built per request, so the gateway keeps one ledger per session on
the session bus (like ``todo_store``) and passes it in.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Stamp = tuple[int, int]
WriteProblem = Literal["read_before_write", "stale_read"]

_RECENT_COMMANDS = 20


def stamp_of(path: Path) -> Stamp | None:
    """``(mtime_ns, size)`` of ``path``, or ``None`` when it cannot be stat'ed."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


@dataclass(frozen=True)
class CommandRecord:
    command: str
    exit_code: int


class FileReadLedger:
    """Which files were read (and in what state), plus what this session wrote."""

    def __init__(self) -> None:
        self._seen: dict[str, Stamp] = {}
        self._written: set[str] = set()
        self._commands: deque[CommandRecord] = deque(maxlen=_RECENT_COMMANDS)

    @property
    def written(self) -> frozenset[str]:
        """Repo-relative paths written this session; survives :meth:`clear`."""
        return frozenset(self._written)

    def record_read(self, key: str, stamp: Stamp | None) -> None:
        if stamp is not None:
            self._seen[key] = stamp

    def record_write(self, key: str, stamp: Stamp | None) -> None:
        self._written.add(key)
        if stamp is not None:
            self._seen[key] = stamp

    def forget(self, key: str) -> None:
        """Drop ``key`` after the agent deleted or moved the file away."""
        self._seen.pop(key, None)
        self._written.add(key)

    def check_write(self, key: str, current: Stamp | None) -> WriteProblem | None:
        """Why an edit to the existing file ``key`` must be refused, or ``None``."""
        seen = self._seen.get(key)
        if seen is None:
            return "read_before_write"
        if current is not None and current != seen:
            return "stale_read"
        return None

    def clear(self) -> None:
        """Forget every read — called when history compaction drops file contents."""
        self._seen.clear()

    @property
    def recent_commands(self) -> tuple[CommandRecord, ...]:
        """Commands recorded this session, oldest first. Survives :meth:`clear`."""
        return tuple(self._commands)

    def record_command(self, command: str, exit_code: int) -> None:
        self._commands.append(CommandRecord(command=command, exit_code=exit_code))

    def last_command_matching(self, patterns: tuple[str, ...]) -> CommandRecord | None:
        """Most recent recorded command whose line matches any regex in ``patterns``."""
        compiled = [re.compile(p) for p in patterns]
        for record in reversed(self._commands):
            if any(rx.search(record.command) for rx in compiled):
                return record
        return None
