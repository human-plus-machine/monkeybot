"""Stable command-executor surface for foreground and background jobs.

``TerminalExecutor`` and ``SandboxExecutor`` both implement this protocol.
The SF adapter overrides ``execute`` today; background builds must override
these methods too (or inherit them) so a broker session stays alive until the
job exits, is killed, or ``aclose`` runs. MonkeyBot does not talk to the
build broker itself.

``aclose`` returns the jobs it killed. An empty list means nothing was still
running. A second call is a no-op and returns an empty list.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from monkeybot.core.tools.background_jobs import BackgroundJob
from monkeybot.core.tools.terminal import ExecutionResult


class BackgroundCommandExecutor(Protocol):
    """Foreground ``execute`` plus detached start / status / output / kill."""

    def running_jobs(self) -> list[BackgroundJob]:
        """Jobs whose status is still ``running``."""
        ...

    def has_job(self, job_id: str) -> bool:
        """True when this executor started ``job_id``."""
        ...

    async def execute(
        self,
        command: str,
        args: list[str],
        *,
        timeout: int = 60,
        cwd: Path | str | None = None,
        extra_allowed_commands: Sequence[str] | None = None,
    ) -> ExecutionResult:
        """Run a command and block until it exits or times out."""
        ...

    async def start_background(
        self,
        command: str,
        args: list[str],
        *,
        timeout: int = 60,
        cwd: Path | str | None = None,
        extra_allowed_commands: Sequence[str] | None = None,
        log_dir: Path | None = None,
    ) -> BackgroundJob:
        """Start a detached command and return its job handle immediately."""
        ...

    async def get_status(self, job_id: str) -> BackgroundJob:
        """Refresh and return the job. Raises ``JobNotFoundError`` for an unknown id."""
        ...

    async def read_output(self, job_id: str, cursor: int) -> tuple[str, int]:
        """Return ``(new_text, new_cursor)`` from the job log after ``cursor``."""
        ...

    async def kill(self, job_id: str) -> BackgroundJob:
        """Stop a running job. Already-finished jobs are returned unchanged."""
        ...

    async def aclose(self) -> list[BackgroundJob]:
        """Kill remaining jobs and release executor resources."""
        ...
