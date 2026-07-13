"""Convert agent-facing messages into the provider-facing payload.

Pipeline stage after :func:`transform_context`. Resolves attachments and applies
optional pressure-tier tool-result shaping without mutating persisted history.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from monkeybot.core.attachments.resolve import resolve_messages_for_provider
from monkeybot.core.attachments.store import AttachmentStore
from monkeybot.core.context.tool_shapers import shape_messages_tool_results
from monkeybot.core.llm.provider import Message


def convert_to_provider(
    messages: Sequence[Message],
    *,
    attachment_store: AttachmentStore | None,
    session_id: str,
    pressure_tier: Literal["none", "moderate", "aggressive"] = "none",
    protect_recent: int = 6,
) -> list[Message]:
    """Agent messages → provider-ready messages (attachments resolved, shaped).

    Persisted history is never mutated; shaping and resolution are view-only.
    """
    working: Sequence[Message] = messages
    if pressure_tier in ("moderate", "aggressive"):
        working = shape_messages_tool_results(
            working,
            protect_recent=protect_recent,
            pressure_tier=pressure_tier,
        )
    return resolve_messages_for_provider(
        working,
        attachment_store=attachment_store,
        session_id=session_id,
    )
