"""Legacy import path for SQLite persistence helpers.

Prefer :mod:`monkeybot.core.persistence.sqlite` for new code. This module
re-exports the same symbols that lived here before ``db.py`` was renamed to
``sqlite.py``.
"""

from __future__ import annotations

from monkeybot.core.persistence.sqlite import (
    DEFAULT_DB_URL,
    SCHEMA_DDLS,
    _LEGACY_SCHEMA_MESSAGE,
    apply_schema,
    configure_connection,
    open_connection,
    sqlite_path_from_db_url,
)

__all__ = [
    "DEFAULT_DB_URL",
    "SCHEMA_DDLS",
    "_LEGACY_SCHEMA_MESSAGE",
    "apply_schema",
    "configure_connection",
    "open_connection",
    "sqlite_path_from_db_url",
]
