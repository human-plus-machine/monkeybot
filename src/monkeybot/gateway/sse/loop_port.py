"""
Injectable ports between the SSE gateway and the agent loop / usage store.
"""

from typing import Any, Protocol

from monkeybot.core.types.content_blocks import ContentBlock


class LoopPort(Protocol):
    """Schedules a turn; implementations publish Agent events to the session bus."""

    async def start_turn(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> None:
        """Schedule background work; events MUST be published to that session's bus."""


class UsagePort(Protocol):
    """Returns usage aggregates for GET /sessions/{id}/usage."""

    async def session_usage(
        self,
        session_id: str,
        *,
        since: str | None,
    ) -> dict[str, Any]:
        """Return JSON-serializable dict matching GET /usage body."""
