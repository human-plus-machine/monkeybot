-- Conversation branch lineage (edit / regenerate / rewind).
-- Apply when paths.auto_schema is false (migration-owned schema).
--
-- Postgres syntax below. For SQLite use INTEGER for the BIGINT columns,
--   INTEGER (0/1) for is_active, and `WHERE is_active = 1` on the index.
-- Firestore: no migration; the session_branches collection is created on write.

CREATE TABLE IF NOT EXISTS session_branches (
    agent_scope TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    parent_branch_id TEXT,
    fork_row_id TEXT,
    op TEXT,
    created_at BIGINT NOT NULL,
    last_active_at BIGINT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (agent_scope, session_id, branch_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_session_branches_active
    ON session_branches(agent_scope, session_id) WHERE is_active;
