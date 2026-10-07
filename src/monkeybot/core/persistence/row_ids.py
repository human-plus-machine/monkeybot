"""Stable ids for stored history rows (``Message.row_id``)."""

from __future__ import annotations

import uuid

from monkeybot.core.llm.provider import Message

# Prefix for rows written before the row_id column existed. The suffix is the
# row's storage key, which stays fixed until the row is rewritten; a rewrite
# persists the derived id, so it stays fixed from then on.
LEGACY_ROW_ID_PREFIX = "legacy:"


def new_row_id() -> str:
    return uuid.uuid4().hex


def row_id_for_insert(message: Message) -> str:
    """The id to store for ``message``: its own if it has one, else a new one."""
    return message.row_id or new_row_id()


def loaded_row_id(stored: object, storage_key: object) -> str:
    """The id of a loaded row, deriving one from ``storage_key`` for legacy rows."""
    if isinstance(stored, str) and stored:
        return stored
    return f"{LEGACY_ROW_ID_PREFIX}{storage_key}"
