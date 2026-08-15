"""History append + durable outbox enqueue."""

from __future__ import annotations

import logging
from typing import Any

from ulid import ULID

from monkeybot.core.llm.provider import Message
from monkeybot.core.memory.ids import conversation_wing
from monkeybot.core.memory.observability import current_traceparent, log_event, memory_span
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.persistence.errors import AmbiguousCommitError
from monkeybot.core.types.content_blocks import Text

logger = logging.getLogger(__name__)


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


async def _append_history(
    history: HistoryStore,
    thread_id: str,
    message: Message,
    *,
    turn_id: str,
    message_id: str,
) -> None:
    await history.append(thread_id, message, turn_id=turn_id, message_id=message_id)


async def _append_atomic(
    append_with: Any,
    thread_id: str,
    message: Message,
    *,
    turn_id: str,
    message_id: str,
    outbox: dict[str, Any],
) -> None:
    """Persist history + outbox using ``message_id`` as the idempotency key."""
    await append_with(
        thread_id,
        message,
        turn_id=turn_id,
        message_id=message_id,
        outbox=outbox,
    )


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
    """Append a history row and, when ``ingest`` is set, enqueue a memory outbox row.

    History is canonical. An outbox failure after a successful history commit is
    logged and does not fail the turn.
    """
    mid = message_id or new_message_id()
    text = visible_text(message) if ingest else ""
    should_ingest = bool(ingest and _ingest_enabled(memory) and text)
    append_with = getattr(history, "append_with_outbox", None)
    if should_ingest and callable(append_with):
        assert memory is not None
        outbox = memory.outbox_spec(
            thread_id=thread_id,
            turn_id=turn_id,
            message_id=mid,
            role=message.role,
            content=text,
            traceparent=current_traceparent(),
        )
        with memory_span(
            "monkeybot.memory.outbox.enqueue",
            **{
                "memory.operation": "enqueue",
                "memory.role": message.role,
                "memory.wing": conversation_wing(workspace_id_from_env()),
                "memory.backend": getattr(memory, "backend", "chroma"),
            },
        ):
            try:
                await _append_atomic(
                    append_with,
                    thread_id,
                    message,
                    turn_id=turn_id,
                    message_id=mid,
                    outbox=outbox,
                )
            except Exception as exc:
                if isinstance(exc, AmbiguousCommitError):
                    logger.warning(
                        "atomic history+outbox acknowledgement lost; "
                        "retrying idempotently (message_id=%s): %r",
                        mid,
                        exc,
                    )
                    try:
                        await _append_atomic(
                            append_with,
                            thread_id,
                            message,
                            turn_id=turn_id,
                            message_id=mid,
                            outbox=outbox,
                        )
                    except Exception as retry_exc:
                        logger.warning(
                            "idempotent history+outbox retry failed; "
                            "falling back to history-only: %r",
                            retry_exc,
                        )
                        await _append_history(
                            history, thread_id, message, turn_id=turn_id, message_id=mid
                        )
                        return
                else:
                    logger.warning(
                        "atomic history+outbox failed; falling back to history-only: %r",
                        exc,
                    )
                    await _append_history(
                        history, thread_id, message, turn_id=turn_id, message_id=mid
                    )
                    return
            log_event("outbox_enqueue", memory_role=message.role, memory_status="ok")
            memory.wake_writer()
        return
    await _append_history(history, thread_id, message, turn_id=turn_id, message_id=mid)
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
            try:
                await memory.enqueue(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    message_id=mid,
                    role=message.role,
                    content=text,
                    traceparent=current_traceparent(),
                )
            except Exception as exc:
                logger.warning("memory outbox enqueue failed after history commit: %r", exc)
                log_event(
                    "outbox_enqueue",
                    memory_role=message.role,
                    memory_status="error",
                    memory_error_class=type(exc).__name__,
                )
                return
            log_event("outbox_enqueue", memory_role=message.role, memory_status="ok")
            memory.wake_writer()
