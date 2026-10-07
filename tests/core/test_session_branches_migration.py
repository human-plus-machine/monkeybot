"""Upgrade the pre-row-id session_branches table on gateway startup."""

from __future__ import annotations

import pytest

from monkeybot.core.persistence.branches import SQLiteBranchStore
from monkeybot.core.persistence.sqlite import (
    apply_schema,
    backfill_legacy_agent_scope,
    open_connection,
)

_LEGACY_SESSION_BRANCHES_DDL = """
CREATE TABLE session_branches (
    branch_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    parent_branch_id TEXT,
    fork_row_index INTEGER,
    fork_fingerprint TEXT,
    op TEXT,
    created_at INTEGER NOT NULL,
    last_active_at INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, branch_id)
)
"""


async def _column_names(conn, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    await cur.close()
    return {str(row[1]) for row in rows}


async def _index_names(conn) -> set[str]:
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'session_branches'"
    )
    rows = await cur.fetchall()
    await cur.close()
    return {str(row[0]) for row in rows}


@pytest.mark.asyncio
async def test_apply_schema_rebuilds_pre_row_id_session_branches() -> None:
    conn = await open_connection("sqlite:///:memory:")
    try:
        await conn.execute(_LEGACY_SESSION_BRANCHES_DDL)
        await conn.execute(
            """CREATE UNIQUE INDEX idx_session_branches_one_active
            ON session_branches(session_id) WHERE is_active = 1"""
        )
        await conn.execute(
            """INSERT INTO session_branches (
                branch_id, session_id, thread_id, parent_branch_id,
                fork_row_index, fork_fingerprint, op, created_at, last_active_at, is_active
            ) VALUES
                ('root', 's1', 's1', NULL, NULL, NULL, NULL, 10, 10, 0),
                ('b2', 's1', 'branch:s1:b2', 'root', 4, 'abc', 'regenerate', 20, 30, 1)
            """
        )
        await conn.commit()

        await apply_schema(conn)

        names = await _column_names(conn, "session_branches")
        assert "agent_scope" in names
        assert "fork_row_id" in names
        assert "fork_row_index" not in names
        assert "fork_fingerprint" not in names
        indexes = await _index_names(conn)
        assert "idx_session_branches_active" in indexes
        assert "idx_session_branches_one_active" not in indexes

        cur = await conn.execute(
            """SELECT agent_scope, branch_id, thread_id, parent_branch_id, fork_row_id, op, is_active
            FROM session_branches ORDER BY created_at"""
        )
        rows = await cur.fetchall()
        await cur.close()
        assert rows == [
            ("", "root", "s1", None, None, None, 0),
            ("", "b2", "branch:s1:b2", "root", None, "regenerate", 1),
        ]

        await apply_schema(conn)
        cur = await conn.execute("SELECT COUNT(*) FROM session_branches")
        count = await cur.fetchone()
        await cur.close()
        assert count is not None
        assert count[0] == 2
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_backfill_claims_rebuilt_session_branches() -> None:
    conn = await open_connection("sqlite:///:memory:")
    try:
        await conn.execute(_LEGACY_SESSION_BRANCHES_DDL)
        await conn.execute(
            """INSERT INTO session_branches (
                branch_id, session_id, thread_id, parent_branch_id,
                fork_row_index, fork_fingerprint, op, created_at, last_active_at, is_active
            ) VALUES ('root', 's1', 's1', NULL, NULL, NULL, NULL, 10, 10, 1)"""
        )
        await conn.commit()
        await apply_schema(conn)
        await backfill_legacy_agent_scope(conn, "default")

        scoped = SQLiteBranchStore(conn, agent_scope="default")
        rows = await scoped.list("s1")
        assert [row.branch_id for row in rows] == ["root"]
        assert rows[0].fork_row_id is None
        assert rows[0].is_active is True

        unscoped = SQLiteBranchStore(conn, agent_scope="")
        assert await unscoped.list("s1") == []
    finally:
        await conn.close()
