"""Upgrade the pre-row-id session_branches table on gateway startup."""

from __future__ import annotations

import logging

import pytest

from monkeybot.core.persistence.branches import BranchRecord, SQLiteBranchStore
from monkeybot.core.persistence.sqlite import (
    _column_names,
    _rebuild_legacy_session_branches,
    apply_schema,
    backfill_legacy_agent_scope,
    open_connection,
    warn_if_legacy_unscoped_history,
)
from monkeybot.core.runtime.history_rewrite import divergence_points

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
            ("", "b2", "branch:s1:b2", None, None, "regenerate", 1),
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


async def _legacy_conn(rows_sql: str):
    conn = await open_connection("sqlite:///:memory:")
    await conn.execute(_LEGACY_SESSION_BRANCHES_DDL)
    await conn.execute(
        """INSERT INTO session_branches (
            branch_id, session_id, thread_id, parent_branch_id,
            fork_row_index, fork_fingerprint, op, created_at, last_active_at, is_active
        ) VALUES """
        + rows_sql
    )
    await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_rebuild_skips_table_another_gateway_already_rebuilt() -> None:
    conn = await _legacy_conn("('root', 's1', 's1', NULL, NULL, NULL, NULL, 10, 10, 1)")
    try:
        await apply_schema(conn)
        await backfill_legacy_agent_scope(conn, "default")
        await conn.execute(
            "UPDATE session_branches SET fork_row_id = 'r1' WHERE branch_id = 'root'"
        )
        await conn.commit()

        # A second gateway that read the legacy shape before the first rebuilt it.
        await _rebuild_legacy_session_branches(conn)

        cur = await conn.execute("SELECT agent_scope, fork_row_id FROM session_branches")
        rows = await cur.fetchall()
        await cur.close()
        assert rows == [("default", "r1")]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_rebuild_keeps_parent_for_fork_before_first_row() -> None:
    conn = await _legacy_conn(
        "('root', 's1', 's1', NULL, NULL, NULL, NULL, 10, 10, 0),"
        "('b2', 's1', 'branch:s1:b2', 'root', NULL, NULL, 'edit', 20, 30, 1)"
    )
    try:
        await apply_schema(conn)
        cur = await conn.execute(
            "SELECT parent_branch_id FROM session_branches WHERE branch_id = 'b2'"
        )
        row = await cur.fetchone()
        await cur.close()
        assert row == ("root",)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_rebuilt_positioned_fork_does_not_anchor_on_first_message() -> None:
    conn = await _legacy_conn(
        "('root', 's1', 's1', NULL, NULL, NULL, NULL, 10, 10, 0),"
        "('b2', 's1', 'branch:s1:b2', 'root', 4, 'abc', 'regenerate', 20, 30, 1)"
    )
    try:
        await apply_schema(conn)
        await backfill_legacy_agent_scope(conn, "default")
        records: list[BranchRecord] = await SQLiteBranchStore(conn, agent_scope="default").list(
            "s1"
        )
        assert divergence_points(records, "b2", []) == []
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_backfill_leaves_branches_whose_history_another_agent_owns() -> None:
    conn = await _legacy_conn(
        "('root', 's1', 's1', NULL, NULL, NULL, NULL, 10, 10, 1),"
        "('root', 's2', 's2', NULL, NULL, NULL, NULL, 10, 10, 1)"
    )
    try:
        await apply_schema(conn)
        await conn.execute(
            "INSERT INTO conversation_history (thread_id, role, content, created_at, agent_scope) "
            "VALUES ('s1', 'user', 'hi', 1, 'other')"
        )
        await conn.commit()
        await backfill_legacy_agent_scope(conn, "default")

        cur = await conn.execute(
            "SELECT session_id, agent_scope FROM session_branches ORDER BY session_id"
        )
        rows = await cur.fetchall()
        await cur.close()
        assert rows == [("s1", ""), ("s2", "default")]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_warn_reports_unscoped_rebuilt_branches(caplog: pytest.LogCaptureFixture) -> None:
    conn = await _legacy_conn("('root', 's1', 's1', NULL, NULL, NULL, NULL, 10, 10, 1)")
    try:
        await apply_schema(conn)
        with caplog.at_level(logging.WARNING):
            await warn_if_legacy_unscoped_history(conn)
        assert any("session_branches" in r.getMessage() for r in caplog.records)
    finally:
        await conn.close()
