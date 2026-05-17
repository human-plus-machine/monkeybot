"""Apply MonkeyBot SQLite schema DDL idempotently (stdlib sqlite3)."""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys

from monkeybot.core.persistence.sqlite import (
    _LEGACY_SCHEMA_MESSAGE,
    SCHEMA_DDLS,
    sqlite_path_from_db_url,
)

logger = logging.getLogger(__name__)


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")


def _sync_connection_db_path(conn: sqlite3.Connection) -> str:
    cur = conn.execute("PRAGMA database_list")
    rows = cur.fetchall()
    for row in rows:
        if len(row) >= 3 and str(row[1]) == "main":
            file_col = row[2]
            return ":memory:" if not file_col else str(file_col)
    return ":memory:"


def _log_legacy_schema_error_sync(conn: sqlite3.Connection) -> None:
    path = _sync_connection_db_path(conn)
    logger.error("Legacy conversation_history schema detected; db=%s", path)


def _apply_schema_sync(conn: sqlite3.Connection) -> None:
    for ddl in SCHEMA_DDLS:
        conn.execute(ddl)
    conn.commit()
    cur = conn.execute("PRAGMA table_info(conversation_history)")
    col_names = {str(r[1]) for r in cur.fetchall()}
    if "tool_name" in col_names or "tool_call_id" in col_names:
        _log_legacy_schema_error_sync(conn)
        raise RuntimeError(_LEGACY_SCHEMA_MESSAGE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create MonkeyBot SQLite schema.")
    parser.add_argument(
        "--db",
        default=None,
        help="Database file path (default: path resolved from DB_URL)",
    )
    ns = parser.parse_args(argv)
    try:
        path = ns.db if ns.db is not None else sqlite_path_from_db_url(os.environ.get("DB_URL"))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    conn = sqlite3.connect(path)
    try:
        _configure(conn)
        _apply_schema_sync(conn)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
