"""In-memory history doubles shared by loop tests."""

from __future__ import annotations

from monkeybot.core.llm.provider import Message


class FakeHistory:
    def __init__(self, preload: list[Message] | None = None) -> None:
        self.rows: list[Message] = list(preload) if preload is not None else []
        self.reset_calls: list[tuple[str, list[Message]]] = []

    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        del thread_id
        if limit is None:
            return list(self.rows)
        return list(self.rows[-limit:]) if limit > 0 else []

    async def append(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        del thread_id, turn_id, message_id
        self.rows.append(message)

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        self.reset_calls.append((thread_id, list(messages)))
        self.rows = list(messages)


class HookFakeHistory:
    """``test_loop_hooks`` copy. Ignores ``limit`` and does not record resets.

    Not merged with ``FakeHistory`` (D7): ``load(..., limit=n)`` still returns every row.
    """

    def __init__(self) -> None:
        self.rows: list[Message] = []

    async def load(self, thread_id: str, limit: int = 100) -> list[Message]:
        del thread_id, limit
        return list(self.rows)

    async def append(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        del thread_id, turn_id, message_id
        self.rows.append(message)

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        del thread_id
        self.rows = list(messages)
