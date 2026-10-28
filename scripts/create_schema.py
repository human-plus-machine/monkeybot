"""Apply the monkeybot SQLite schema the same way startup does."""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import threading

from monkeybot.core.persistence.sqlite import apply_schema, open_connection, sqlite_path_from_db_url


def _url_for(path: str) -> str:
    if path == ":memory:":
        return "sqlite:///:memory:"
    return f"sqlite:///{path}"


def _apply(url: str) -> None:
    async def _run() -> None:
        conn = await open_connection(url)
        try:
            await apply_schema(conn)
        finally:
            await conn.close()

    def _worker() -> None:
        asyncio.run(_run())

    # The contract test calls main() from a running loop. asyncio.run cannot
    # start on that thread, and apply_schema is the one schema path.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _worker()
        return

    errors: list[BaseException] = []

    def _threaded() -> None:
        try:
            _worker()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=_threaded)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create monkeybot SQLite schema.")
    parser.add_argument(
        "--db",
        default=None,
        help="Database file path (default: path resolved from DB_URL)",
    )
    ns = parser.parse_args(argv)
    try:
        if ns.db is not None:
            url = _url_for(ns.db)
        else:
            url = _url_for(sqlite_path_from_db_url(os.environ.get("DB_URL")))
        _apply(url)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
