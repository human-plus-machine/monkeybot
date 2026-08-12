"""History append + durable outbox enqueue."""

from __future__ import annotations

from typing import Any

from ulid import ULID

from monkeybot.core.llm.provider import Message
from monkeybot.core.memory.ids import conversation_wing
from monkeybot.core.memory.observability import current_traceparent, log_event, memory_span
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.types.content_blocks import Text


def visible_text(message: Message) -> str:
    """User-visible text only (skip thinking and tool-request blocks)."""
    parts = [b.text for b in message.content if isinstance(b, Text) and b.text.strip()]
    return "\n".join(parts).strip()


def new_message_id() -> str:
    return str(ULID())


def workspace_id_from_env() -> str | None:
    import os

    raw = os.environ.get("MONKEYBOT_WORKSPACE_ID", "").strip()
    return raw or None


def _ingest_enabled(memory: Any | None) -> bool:
    return memory is not None and getattr(memory, "ingest_enabled", True)


async def persist_message(
    history: HistoryStore,
    message: Message,
    *,
    thread_id: str,
    turn_id: str,
    memory: Any | None,
    ingest: bool,
    message_id: str | None = None,
) -> None:
    """Append a history row and, when ``ingest`` is set, enqueue a memory outbox row."""
    mid = message_id or new_message_id()
    text = visible_text(message) if ingest else ""
    should_ingest = bool(ingest and _ingest_enabled(memory) and text)
    append_with = getattr(history, "append_with_outbox", None)
    if should_ingest:
        assert memory is not None
        if callable(append_with):
            with memory_span(
                "monkeybot.memory.outbox.enqueue",
                **{
                    "memory.operation": "enqueue",
                    "memory.role": message.role,
                    "memory.wing": conversation_wing(workspace_id_from_env()),
                    "memory.backend": getattr(memory, "backend", "chroma"),
                },
            ):
                await append_with(
                    thread_id,
                    message,
                    turn_id=turn_id,
                    message_id=mid,
                    outbox=memory.outbox_spec(
                        thread_id=thread_id,
                        turn_id=turn_id,
                        message_id=mid,
                        role=message.role,
                        content=text,
                        traceparent=current_traceparent(),
                    ),
                )
                log_event("outbox_enqueue", memory_role=message.role, memory_status="ok")
                memory.wake_writer()
            return
    await history.append(thread_id, message, turn_id=turn_id, message_id=mid)
    if should_ingest:
        assert memory is not None
        with memory_span(
            "monkeybot.memory.outbox.enqueue",
            **{
                "memory.operation": "enqueue",
                "memory.role": message.role,
                "memory.wing": conversation_wing(workspace_id_from_env()),
            },
        ):
            await memory.enqueue(
                thread_id=thread_id,
                turn_id=turn_id,
                message_id=mid,
                role=message.role,
                content=text,
                traceparent=current_traceparent(),
            )
            log_event("outbox_enqueue", memory_role=message.role, memory_status="ok")
            memory.wake_writer()
