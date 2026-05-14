"""Apply MonkeyBot SQLite schema DDL idempotently (stdlib sqlite3)."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from monkeybot.core.db import SCHEMA_DDLS, sqlite_path_from_db_url


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")


def _apply_schema_sync(conn: sqlite3.Connection) -> None:
    for ddl in SCHEMA_DDLS:
        conn.execute(ddl)
    conn.commit()
    cur = conn.execute("PRAGMA table_info(conversation_history)")
    cols = [r[1] for r in cur.fetchall()]
    if "tool_name" not in cols:
        conn.execute("ALTER TABLE conversation_history ADD COLUMN tool_name TEXT")
    conn.commit()


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
    except sqlite3.Error as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
